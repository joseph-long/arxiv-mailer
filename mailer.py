#!/usr/bin/env python
import datetime
import os
import os.path
import pickle
import re
import logging
import sys
import unicodedata
from requests.exceptions import ReadTimeout
import smtplib
import ssl
# https://stackoverflow.com/questions/33857698/sending-email-from-python-using-starttls
_DEFAULT_CIPHERS = (
    'ECDH+AESGCM:DH+AESGCM:ECDH+AES256:DH+AES256:ECDH+AES128:DH+AES:ECDH+HIGH:'
    'DH+HIGH:ECDH+3DES:DH+3DES:RSA+AESGCM:RSA+AES:RSA+HIGH:RSA+3DES:!aNULL:'
    '!eNULL:!MD5'
)
from dateutil.parser import parse
from dateutil import tz
from bs4 import BeautifulSoup
import feedparser
import jinja2
import requests

log = logging.getLogger(__name__)
DEMO_MODE = False

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
    text = re.sub(r'[^\w]', ' ', text)
    # thanks to https://stackoverflow.com/a/29247821
    text = unicodedata.normalize("NFKD", text.casefold())
    text = text.strip()
    return text

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
        if wrap.select_one('h5') is not None:
            position = wrap.select_one('h5').text
        else:
            position = ''
        people[name_parts] = {
            'role': POSTDOC,
            'position': position,
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
    normalized_name = normalize_caseless(name)
    name_match = NAME_RE.match(normalized_name)
    if not name_match:
        log.warn(f"Unable to parse {normalized_name=} with regex")
        return None, 0
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
        ('rodrigo', 'marco navarro'): None
    }
    assert approximate_name_lookup('edgar ferris', people) == (('ferris', 'edgar'), 2)
    assert approximate_name_lookup('bob dave', people) == (('dave', 'a. bob c.'), 2)
    assert approximate_name_lookup('G. Hausschuh', people) == (('hausschuh', 'georgina'), 1)
    assert approximate_name_lookup('{M. Navarro Rodrigo}', people) == (('rodrigo', 'marco navarro'), 1)

def unpack_feed_entry(post, people):
    title, arxiv_id_ext, arxiv_area, update_kind = re.match(r'^(.+) \(arXiv:(.+) \[(.+)\](.*)\)', post.title).groups()
    if len(update_kind):
        # no 'UPDATED' posts, just new stuff please
        return
    author_names = [x.text for x in BeautifulSoup(post.author, features="lxml").select('a')]
    authors = [(name, approximate_name_lookup(name, people)) for name in author_names]
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
    feed = feedparser.parse('https://arxiv.org/rss/astro-ph')
    posts = []
    all_authors = []
    update_day = parse(feed.feed['updated']).astimezone(datetime.timezone.utc).date()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if (update_day - today).days != 0:
        log.warn(f"Mailer was invoked but feed was last updated on {update_day} UTC")
        sys.exit(1)
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
    if DEMO_MODE:
        with open('mailing.eml', 'wb') as f:
            f.write(bytes(msg))
    return msg

def send_email(msg):
    host = os.environ['MAIL_SERVER']
    port = int(os.environ['MAIL_PORT'])
    user = os.environ['MAIL_USERNAME']
    password = os.environ['MAIL_PASSWORD']

    # only TLSv1 or higher
    context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    context.options |= ssl.OP_NO_SSLv2
    context.options |= ssl.OP_NO_SSLv3

    context.set_ciphers(_DEFAULT_CIPHERS)
    context.set_default_verify_paths()
    context.verify_mode = ssl.CERT_REQUIRED
    smtp_server = smtplib.SMTP_SSL(host, port=port, context=context)
    smtp_server.login(user, password)
    smtp_server.send_message(msg)

def main():
    global DEMO_MODE
    run_time = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    tzmst = tz.gettz('America/Phoenix')
    run_time_local = run_time.astimezone(tzmst)

    if len(sys.argv) > 1:
        args = sys.argv[1:]
        if '-d' in args:
            DEMO_MODE = True
    if DEMO_MODE and os.path.exists('./demo.pickle'):
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
            'run_time': run_time_local.strftime('%Y-%m-%d %H:%M %Z'),
        }
        if DEMO_MODE:
            with open('./demo.pickle', 'wb') as f:
                pickle.dump(context, f)
    
    html_mailing, text_mailing = render_mailing(context)
    if DEMO_MODE:
        with open('./mailing.html', 'w') as f:
            f.write(html_mailing)
        with open('./mailing.txt', 'w') as f:
            f.write(text_mailing)

    # Compose the email
    from_addr_spec = os.environ['MAIL_USERNAME'] if not DEMO_MODE else 'astro-stewarxiv@list.arizona.edu'
    from_addr = Address("StewarXiv", addr_spec=from_addr_spec)
    to_addr_spec = os.environ['MAIL_SENDTO'] if not DEMO_MODE else 'astro-stewarxiv@list.arizona.edu'
    to_addrs = [
        Address("StewarXiv", addr_spec=to_addr_spec)
    ]
    subject = f'Today\'s update: {len(posts)} {"preprint" if len(posts) == 1 else "preprints"} from {len(all_authors)} {"colleague" if len(all_authors) == 1 else "colleagues"}'
    msg = compose_email(from_addr, to_addrs, subject, html_mailing, text_mailing)
    # Send the email
    if not DEMO_MODE:
        send_email(msg)

    # Finally: hit the arxiv-vanity URL for each paper so their cache is
    # all warmed up
    if not DEMO_MODE:
        for post in posts:
            try:
                requests.get(f"https://www.arxiv-vanity.com/papers/{post['arxiv_id']}/", timeout=5)
            except ReadTimeout:
                pass

if __name__ == "__main__":
    logging.basicConfig(level='WARN')
    log.setLevel('DEBUG')
    main()
