#!/bin/bash

export MAIL_SERVER=smtp.gmail.com
export MAIL_PORT=465
export MAIL_USERNAME=lwhitler@arizona.edu
export MAIL_PASSWORD='halt!backer!varmint'
export MAIL_SENDTO=astro-stewarxiv@list.arizona.edu

conda activate arxiv-mailer
python /home/lwhitler/Documents/miscellaneous/arxiv-mailer/mailer.py
