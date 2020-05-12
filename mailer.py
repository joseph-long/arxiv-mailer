#!/usr/bin/env python
import datetime
import os.path
import pickle
import re
import sys
import unicodedata
from urllib3.exceptions import ReadTimeoutError

from bs4 import BeautifulSoup
import feedparser
import jinja2
import requests

def soupify(url):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        req = requests.get(url, verify=False)
    return BeautifulSoup(req.text, features="lxml")

FACULTY = 1
POSTDOC = 2
STUDENT = 3

def normalize_caseless(text):
    # thanks to https://stackoverflow.com/a/29247821
    return unicodedata.normalize("NFKD", text.casefold())

def build_directory():
    people = {}

    faculty_page = soupify('https://www.as.arizona.edu/people/faculty')
    for facwrap in faculty_page.select('.faculty_wrapper'):
        name_parts = tuple(normalize_caseless(part.strip()) for part in facwrap.select_one('h4').text.split(',', 1))
        people[name_parts] = {
            'role': FACULTY,
            'position': facwrap.select_one('h5').text,
            'image': facwrap.select_one('img')['src'].rsplit('?', 1)[0]
        }

    postdoc_page = soupify('https://www.as.arizona.edu/people/postdoctoral')
    for wrap in postdoc_page.select('.view-people tr'):
        name_parts = tuple(normalize_caseless(part.strip()) for part in wrap.select_one('h4').text.split(',', 1))
        people[name_parts] = {
            'role': POSTDOC,
            'position': wrap.select_one('h5').text,
            'image': wrap.select_one('img')['src'].rsplit('?', 1)[0]
        }

    student_page = soupify('https://www.as.arizona.edu/people/grad_students')
    for wrap in student_page.select('.view-people tr'):
        first_names, last_name = tuple(normalize_caseless(part.strip()) for part in wrap.select_one('h4').text.rsplit(' ', 1))
        people[(last_name, first_names)] = {
            'role': STUDENT,
            'position': wrap.select_one('h5').text,
            'image': wrap.select_one('img')['src'].rsplit('?', 1)[0]
        }
    return people

NAME_RE = re.compile(r'^(?P<first>(?:(?P<initial>\w).*)[\. ]+)+(?P<last>\w.*)$')
def test_name_regex():
    assert NAME_RE.match('J.Long').groupdict() == {'first': 'J.', 'initial': 'J', 'last': 'Long'}
    assert NAME_RE.match('Joseph D. Long').groupdict() == {'first': 'Joseph D. ', 'initial': 'J', 'last': 'Long'}
    assert NAME_RE.match('J. D. Long').groupdict() == {'first': 'J. D. ', 'initial': 'J', 'last': 'Long'}
    assert NAME_RE.match('J Long').groupdict() == {'first': 'J ', 'initial': 'J', 'last': 'Long'}
INITIAL_RE = re.compile(r'^\w(\.|\s|$)')
def test_initial_regex():
    assert INITIAL_RE.match('J. D.')
    assert not INITIAL_RE.match('Jo. D.')
    assert INITIAL_RE.match('J.D.')
    assert INITIAL_RE.match('J')
    assert INITIAL_RE.match('J D')

ALL_INITIALS_RE = re.compile(r'\b\w\.')
def strip_initials(names):
    return ' '.join(ALL_INITIALS_RE.sub('', names).split())
def test_strip_initials():
    assert strip_initials('J. Long') == 'Long'

def approximate_name_lookup(name, people):
    # normalize at input boundary so comparisons are simply ==
    name_match = NAME_RE.match(normalize_caseless(name.strip()))
    if not name_match:
        raise ValueError(f"Unable to parse {name} with regex")
    parts = name_match.groupdict()
    first_names = parts['first'].strip()
    first_initial = parts['initial']
    last_name = parts['last'].strip()
    
    for person_last, person_first in people:
        score = 0
        if person_last == last_name:
            # last name matches, but what about first?
            if person_first == first_names:
                # easy: last name matches, first name(s) match
                score = 2
            elif first_names.startswith(person_first):
                score = 2
            elif first_names in person_first:
                # first_names is a substring of person_first
                # does person_first match after removing initials?
                if strip_initials(person_first).startswith(first_names):
                    score = 2
            elif person_first in first_names:
                # does first_names match after removing initials?
                if strip_initials(first_names).startswith(person_first):
                    score = 2
            elif person_first[0] == first_initial[0]:
                # harder: last name matches, first initial matches
                # check if it's an initial (single letter followed by space, period, or end of string
                re_match = INITIAL_RE.match(first_names)
                if re_match:
                    score = 1
                # otherwise, same first initial, different first name, so no match
            # else: same last name, different first name, no match
        if score:
            return (person_last, person_first), score
    return None, 0

def test_approximate_name_lookup():
    people = {
        ('dave', 'a. bob c.'): None,
        ('ferris', 'edgar'): None,
        ('hausschuh', 'georgina'): None,
    }
    assert approximate_name_lookup('edgar ferris', people) == (('ferris', 'edgar'), 2)
    assert approximate_name_lookup('bob dave', people) == (('dave', 'a. bob c.'), 2)
    assert approximate_name_lookup('G. Hausschuh', people) == (('hausschuh', 'georgina'), 1)

def unpack_feed_entry(post, people):
    title, arxiv_id_ext, arxiv_area, update_kind = re.match(r'^(.+) \(arXiv:(.+) \[(.+)\](.*)\)', post.title).groups()
    if len(update_kind):
        # no 'UPDATED' posts, just new stuff please
        return
    authors = [(x.text, approximate_name_lookup(x.text, people)) for x in BeautifulSoup(post.author, features="lxml").select('a')]
    our_people_score = sum(item[1][1] for item in authors)
    if not our_people_score > 1:
        # If only one partial match was found, it's probably not who we think it is.
        # (Multiple first initial + last name matches or a single full name match
        # are sufficient to keep a post.)
        return
    arxiv_id = post.link.rsplit('/', 1)[1]
    abstract = BeautifulSoup(post.summary, features="lxml").text
    arxiv_area = arxiv_area.rsplit('.', 1)
    out = {
        'authors': authors,
        'title': title,
        'area': arxiv_area,
        'abstract': abstract.replace('\n', ' '),
        'arxiv_id': arxiv_id,
    }
    return out

def get_matching_posts(people):
    feed = feedparser.parse('http://arxiv.org/rss/astro-ph')
    posts = []
    all_authors = []
    for post in feed.entries:
        unpacked_post = unpack_feed_entry(post, people)
        if unpacked_post:
            posts.append(unpacked_post)
            for author in unpacked_post['authors']:
                if author[1][0] is not None:
                    key = author[1][0]
                    all_authors.append((key, people[key]))
    # sorting by the key, so by last names
    all_authors.sort()
    all_authors = [x[1] for x in all_authors]
    return posts, all_authors

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    autoescape=jinja2.select_autoescape(['html', 'xml'])
)

def render_mailing(context_dict):
    html_template = env.get_template('mailing.jinja2.html')
    html_mailing = html_template.render(**context_dict)
    text_template = env.get_template('mailing.jinja2.txt')
    text_mailing = text_template.render(**context_dict)
    return html_mailing, text_mailing

from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import make_msgid

def compose_email(from_address, to_addresses, subject, html_mailing, text_mailing):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_address
    msg['To'] = to_addresses
    msg.set_content(text_mailing)
    msg.add_alternative(html_mailing, subtype='html')
    with open('mailing.eml', 'wb') as f:
        f.write(bytes(msg))
    return msg

import smtplib

def send_email(msg):
    with smtplib.SMTP('localhost') as s:
        s.send_message(msg)

def main():
    demo_mode = False
    run_time = datetime.datetime.utcnow()

    if len(sys.argv) > 1:
        args = sys.argv[1:]
        if '-d' in args:
            demo_mode = True
    if demo_mode and os.path.exists('./demo.pickle'):
        with open('./demo.pickle', 'rb') as f:
            context = pickle.load(f)
            # define locals from pickle
            people = context['people']
            posts = context['posts']
            all_authors = context['all_authors']
            # except run_time, update that in loaded dict
            context['run_time'] = run_time
    else:
        people = build_directory()
        posts, all_authors = get_matching_posts(people)
        context = {
            'people': people,
            'posts': posts,
            'all_authors': all_authors,
            'run_time': run_time,
        }
        if demo_mode:
            with open('./demo.pickle', 'wb') as f:
                pickle.dump(context, f)
    
    html_mailing, text_mailing = render_mailing(context)
    with open('./mailing.html', 'w') as f:
        f.write(html_mailing)
    with open('./mailing.txt', 'w') as f:
        f.write(text_mailing)

    # Compose the email
    from_addr = Address("StewarXiv", "josephlong", "email.arizona.edu")
    to_addrs = [
        Address("Joseph Long", "josephlong", "email.arizona.edu")
    ]
    subject = f'StewarXiv update: {len(posts)} {"post" if len(posts) == 1 else "posts"} from {len(all_authors)} {"colleague" if len(all_authors) == 1 else "colleagues"}'
    compose_email(from_addr, to_addrs, subject, html_mailing, text_mailing)
    # Send the email
    if not demo_mode:
        # TODO
        pass

    # Finally: hit the arxiv-vanity URL for each paper so their cache is
    # all warmed up
    if not demo_mode:
        for post in posts:
            try:
                requests.get(f"https://www.arxiv-vanity.com/papers/{post['arxiv_id']}/", timeout=5)
            except ReadTimeoutError:
                pass

if __name__ == "__main__":
    main()