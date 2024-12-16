# Contents

* [Choose your path - easy way or hard way](#choose-path)
* [Setup Database](#setup-database)
* [Install Python Libraries](#install-python-libraries)
* [Install additional requirements](#install-additional-requirements)      
* [Setup pyfedi](#setup-pyfedi)
* [Setup .env file](#setup-env-file)
* [Initialise Database and Setup Admin account](#initialise-database-and-setup-admin-account)
* [Run the app](#run-the-app)
* [Database Management](#database-management)
* [Keeping your local instance up to date](#keeping-your-local-instance-up-to=date)
* [Running PieFed in production](#running-piefed-in-production)
* [Pre-requisites for Mac OS](#pre-requisites-for-mac-os)
* [Notes for Windows (WSL2)](#notes-for-windows-wsl2)        
* [Notes for Pip Package Management](#notes-for-pip-package-management)

<div id="choose-path"></div>

## Do you want this the easy way or the hard way?

### Easy way: docker

Docker can be used to create an isolated environment that is separate from the host server and starts from a consistent
configuration. While it is quicker and easier, it's not to everyone's taste.

* Clone PieFed into a new directory

```bash
git clone https://codeberg.org/rimu/pyfedi.git
```

* Copy suggested docker config

```bash
cd pyfedi
cp env.docker.sample .env.docker
```

* Edit docker environment file

Open .env.docker in your text editor, set SECRET_KEY to something random and set SERVER_NAME to your domain name,
WITHOUT the https:// at the front. The database login details doesn't really need to be changed because postgres will be
locked away inside it's own docker network that only PieFed can access but if you want to change POSTGRES_PASSWORD go ahead
just be sure to update DATABASE_URL accordingly.

Check out compose.yaml and see if it is to your liking. Note the port (8030) and volume definitions - they might need to be
tweaked.

* First startup

This will take a few minutes.

```bash
export DOCKER_BUILDKIT=1
docker-compose up --build
```

After a while the gibberish will stop scrolling past. If you see errors let us know at [https://piefed.social/c/piefed_help](https://piefed.social/c/piefed_help).

* Networking

You need to somehow to allow client connections from outside to access port 8030 on your server. The details of this is outside the scope
of this article. You could use a nginx reverse proxy, a cloudflare zero trust tunnel, tailscale, whatever. Just make sure it has SSL on
it as PieFed assumes you're making requests that start with https://your-domain.

Once you have the networking set up, go to https://your-domain in your browser and see if the docker output in your terminal
shows signs of reacting to the request. There will be an error showing up in the console because we haven't done the next step yet.

* Database initialization

This must be done once and once only. Doing this will wipe all existing data in your instance so do not do it unless you have a
brand new instance.

Open a shell inside the PieFed docker container:

`docker exec -it piefed_app1 sh`

Inside the container, run the initialization command:

```
export FLASK_APP=pyfedi.py
flask init-db
```

Among other things this process will get you set up with a username and password. Don't use 'admin' as the user name, script kiddies love that one.

* The moment of truth

Go to https://your-domain in your web browser and PieFed should appear. Log in with the username and password from the previous step.

At this point docker is pretty much Ok so you don't need to see the terminal output as readily. Hit Ctrl + C to close down docker and then run

```bash
docker-compose up -d
```

to have PieFed run in the background.

* But wait there's more

Until you set the right environment variables, PieFed won't be able to send email. Check out env.sample for some hints.
When you have a new value to set, add it to .env.docker and then restart docker with:

```
docker-compose down && docker-compose up -d
```

There are also regular cron jobs that need to be run. Set up cron on the host to run those scripts inside the container - see the Cron
section of this document for details.

You probably want a Captcha on the registration form - more environment variables.

CDN, CloudFlare. More environment variables.

All this is explained in the bare metal guide, below.

### Hard way: bare metal

Read on

<div id="setup-database"></div>

## Setup Database

#### Install postgresql

PieFed should work on version 13.x or newer. If you have errors running `flask init-db`, check your postrgesql version.

##### Install postgresql 16:

For installation environments that use `apt` as a package manager:

```bash
sudo apt install ca-certificates pkg-config
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt/ $(lsb_release -cs)-pgdg main" >> /etc/apt/sources.list.d/pgdg.list'
sudo apt update
sudo apt install libpq-dev postgresql
```

#### Create new DB user

Choose a username and password. To use 'pyfedi' for both:     
```bash
sudo -iu postgres psql -c "CREATE USER pyfedi WITH PASSWORD 'pyfedi';"
```

#### Create new database

Choose a database name, owned by your new user. For a database called and owned by 'pyfedi':      
```bash
sudo -iu postgres psql -c "CREATE DATABASE pyfedi WITH OWNER pyfedi;"
```

<div id="install-python-libraries"></div>

## Install Python Libraries

[Pre-requisites for Mac OS](#pre-requisites-for-mac-os)         
[Notes for Windows (WSL2)](#notes-for-windows-wsl2)        

For installation environments that use `apt` as a package manager:   
```bash
sudo apt install python3-pip python3-venv python3-dev python3-psycopg2
``` 


<div id="install-additional-requirements"></div>

## Install additional requirements

For installation environments that use 'apt' as a package manager:   

```bash
sudo apt install redis-server
sudo apt install git
sudo apt install tesseract-ocr
```

<div id="setup-pyfedi"></div>

## Setup PyFedi

* Clone PieFed

```bash
git clone https://codeberg.org/rimu/pyfedi.git
```

* cd into pyfedi, set up and enter virtual environment

```bash
cd pyfedi
python3 -m venv ./venv
source venv/bin/activate
```

* Use pip to install requirements           

```bash
pip install wheel
pip install -r requirements.txt
```
(see [Notes for Windows (WSL2)](#windows-wsl2) if appropriate)        

<div id="setup-env-file"></div>

## Setup .env file

* Copy `env.sample` to `.env`
* Edit `.env` to suit your server.
* Using the same username, password, and database name as used when setting up database, set the connection up, something like this:
    ```
    DATABASE_URL=postgresql+psycopg2://username:password@localhost/database_name
    ```
    * Also change `SECRET_KEY` to some random sequence of numbers and letters.
    

### Extra info

* `SERVER_NAME` should be the domain of the site/instance. Use `127.0.0.1:5000` during development unless using ngrok. Just use the bare
domain name, without https:// on the front or a slash on the end.
* `RECAPTCHA_PUBLIC_KEY` and `RECAPTCHA_PRIVATE_KEY` can be generated at https://www.google.com/recaptcha/admin/create (this is optional - omit to allow registration without RECAPCHA).
* `CACHE_TYPE` can be `FileSystemCache` or `RedisCache`. `FileSystemCache` is fine during development (set `CACHE_DIR` to `/tmp/piefed` or `/dev/shm/piefed`)
while `RedisCache` **should** be used in production. If using `RedisCache`, set `CACHE_REDIS_URL` to `redis://localhost:6379/1`. Visit https://yourdomain/testredis to check if your redis url is working.

* `CELERY_BROKER_URL` is similar to `CACHE_REDIS_URL` but with a different number on the end: `redis://localhost:6379/0`

* `MAIL_*` is for sending email using a SMTP server. Leave `MAIL_SERVER` empty to send email using AWS SES instead.

* `AWS_REGION` is the name of the AWS region where you chose to set up SES, if using SES. [SES credentials are stored in `~/.aws/credentials`](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html). That file has a format like

    ```
    [default]
    aws_access_key_id = JKJHER*#KJFFF
    aws_secret_access_key = /jkhejhkrejhkre
    region=ap-southeast-2
    ```
    You can also [use environment variables](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#environment-variables) if you prefer.

* Test email sending by going to https://yourdomain/test_email. It will try to send an email to the current user's email address.
If it does not work check the log file at logs/pyfedi.log for clues.

* BOUNCE_ADDRESS is where email bounces will go to. If BOUNCE_* is configured then all emails in that inbox
will be treated as bounces and deleted after extracting the email addresses in them. Use a dedicated inbox
for bounces, not a inbox you also use for other purposes.

### Development mode

Setting `FLASK_DEBUG=1` in the `.env` file will enable the `<your-site>/dev/tools` page.

That page can be accessed from the `Admin` navigation drop down, or nav bar as `Dev Tools`. That page has buttons that can create/delete communities and topics. The communities and topics will all begin with "dev_".

<div id="initialise-database-and-setup-admin-account"></div>

## Initialise database, and set up admin account

```bash
export FLASK_APP=pyfedi.py
flask db upgrade
flask init-db
```

(choose a new username, email address, and password for your PyFedi admin account)

If you see an error message `ModuleNotFoundError: No module named 'flask_babel'` then use `venv/bin/flask` instead of `flask`
for all flask commands.

<div id="run-the-app"></div>

## Run the app    

```bash
flask run
```
(open web browser at http://127.0.0.1:5000)          
(log in with username and password from admin account)

For development purposes, that should be enough - see ./dev_notes.txt for a few more bits and pieces. Most of what follows is for running PieFed in production.

<div id="database-management"></div>

## Database Management

In future if you use git pull and notice some new files in `migrations/versions/*`, you need to do:    

```bash
source venv/bin/activate #if not already in virtual environment
flask db upgrade
```

#### For Database changes:

create a migration based on recent changes to `app/models.py`:

```bash
flask db migrate -m "users table"
```
     
run migrations:         
```bash
flask db upgrade
```

<div id="keeping-your-local-instance-up-to=date"></div>

## Keeping your local instance up to date

In a development environment, all you need to do is

```bash
git pull
flask db upgrade
```

In production, celery and flask run as background services so they need to be restarted manually. Run the `./deploy.sh` script
to easily restart services at the same time as pulling down changes from git, etc.

<div id="federation-during-development"></div>

## Federation during development

Federation doesn't work without SSL, without a domain name or without your server being accessible from outside your network. So, when running on http://127.0.0.1:5000 you have none of those.

The site will still run without federation. You can create local communities and post in them...

My way around this is to use ngrok.com, which is a quick and simple way to create a temporary VPN with a domain and SSL. The free plan comes with ephermeral domain names that change every few days, which will break federation, or one randomly-named static domain that will need re-launching every few days. $10 per month will get you https://yourwhatever.ngrok.app which won't change. 

Once you have ngrok working, edit the `.env` file and change the `SERVER_NAME` variable to your new domain name (all lower case).

<div id="running-piefed-in-production"></div>

## Running PieFed in production

Running PieFed in production relies on several additional packages that need to be installed.

```bash
source venv/bin/activate #if not already in virtual environment
pip3 install gunicorn celery
```

Copy `celery_worker.default.py` to `celery_worker.py`. Edit `DATABASE_URL` and `SERVER_NAME` to have the same values as in `.env`.

Edit `gunicorn.conf.py` and change `worker_tmp_dir` if needed.

You will want to [tune PostgreSQL](https://pgtune.leopard.in.ua/). [More on this](https://www.enterprisedb.com/postgres-tutorials/how-tune-postgresql-memory).
If you have more than 4 GB of RAM, consider [turning on 'huge pages'](https://www.percona.com/blog/why-linux-hugepages-are-super-important-for-database-servers-a-case-with-postgresql/)
also [see this](https://pganalyze.com/blog/5mins-postgres-tuning-huge-pages).

[PgBouncer](https://www.pgbouncer.org) can be helpful in a high traffic situation.

To assess whether to accept a registration application it can be helpful to know the country of the applicant. This can be
automatically discovered by using [the ipinfo service](https://ipinfo.io/) - register with them to get an API token and put it into your .env file.

If the search function is not returning any results, you need to [add some database triggers](https://codeberg.org/rimu/pyfedi/issues/358#issuecomment-2475019).

<div id="background-services"></div>

### Background services

In production, Gunicorn and Celery need to run as background services:

#### Gunicorn

Create a new file:

```bash
sudo nano /etc/systemd/system/pyfedi.service
```

Add the following to the new file, altering paths as appropriate for your install location

```
[Unit]
Description=Gunicorn instance to serve PieFed application
After=network.target

[Service]
User=rimu
Group=rimu
WorkingDirectory=/home/rimu/pyfedi/
Environment="PATH=/home/rimu/pyfedi/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"
ExecStart=/home/rimu/pyfedi/venv/bin/gunicorn --config gunicorn.conf.py --preload pyfedi:app
ExecReload=/bin/kill -HUP $MAINPID
Restart=always


[Install]
WantedBy=multi-user.target
```
    

#### Celery

Create another file:

```bash
sudo nano /etc/systemd/system/celery.service
```

Add the following, altering as appropriate

```
[Unit]
Description=Celery Service
After=network.target

[Service]
Type=forking
User=rimu
Group=rimu
EnvironmentFile=/etc/default/celeryd
WorkingDirectory=/home/rimu/pyfedi
ExecStart=/bin/sh -c '${CELERY_BIN} multi start -A ${CELERY_APP} ${CELERYD_NODES} --pidfile=${CELERYD_PID_FILE} \
  --logfile=${CELERYD_LOG_FILE} ${CELERYD_OPTS}'
ExecStop=/bin/sh -c '${CELERY_BIN} multi stopwait ${CELERYD_NODES} --pidfile=${CELERYD_PID_FILE}'
ExecReload=/bin/sh -c '${CELERY_BIN} multi restart -A ${CELERY_APP} ${CELERYD_NODES} --pidfile=${CELERYD_PID_FILE} \
  --logfile=${CELERYD_LOG_FILE} ${CELERYD_OPTS}'

[Install]
WantedBy=multi-user.target
```

Create another file:

```
sudo nano /etc/default/celeryd
```

Contents (change paths to suit):

```
# The names of the workers. This example creates one workers
CELERYD_NODES="worker1"

# The name of the Celery App, should be the same as the python file
# where the Celery tasks are defined
CELERY_APP="celery_worker.celery"

# Log and PID directories
CELERYD_LOG_FILE="/var/log/celery/%n%I.log"
CELERYD_PID_FILE="/dev/shm/celery/%n.pid"

# Log level
CELERYD_LOG_LEVEL=INFO

# Path to celery binary, that is in your virtual environment
CELERY_BIN=/home/rimu/pyfedi/venv/bin/celery
CELERYD_OPTS="--autoscale=5,1 --max-tasks-per-child=1000"
```

#### Enable and start background services

```bash
sudo systemctl enable pyfedi.service
sudo systemctl enable celery.service

sudo systemctl start pyfedi.service
sudo systemctl start celery.service
```

Check status of services:

```bash
sudo systemctl status pyfedi.service
sudo systemctl status celery.service
```

Inspect log files at:

* `/var/log/celery/*`
* `/var/log/nginx/*`
* `/your_piefed_installation/logs/pyfedi.log`


### Nginx

You need a reverse proxy that sends all traffic to port 5000. Something like:

```
upstream app_server {
    # fail_timeout=0 means we always retry an upstream even if it failed
    # to return a good HTTP response

    # for UNIX domain socket setups
    # server unix:/tmp/gunicorn.sock fail_timeout=0;

    # for a TCP configuration
    server 127.0.0.1:5000 fail_timeout=0;
    keepalive 4;
}

server {
    server_name piefed.social
    root /whatever

    keepalive_timeout 30;
    ssi off;

    location / {
        # Proxy all requests to Gunicorn
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $http_host;
        proxy_redirect off;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_pass http://app_server;
        ssi off;
    }

    # Serve static files directly with nginx
    location /static/ {
        alias /whatever/app/static/;
        expires max;
        access_log off;
    }

}
```

**_The above is not a complete configuration_** - you will want to add more settings for SSL, etc. See also
https://codeberg.org/rimu/pyfedi/issues/136#issuecomment-1726739

### Cron tasks


To send email reminders about unread notifications, put this in a new file under `/etc/cron.d`

```
1 */6 * * * rimu cd /home/rimu/pyfedi && /home/rimu/pyfedi/email_notifs.sh
```

Change `/home/rimu/pyfedi` to the location of your installation and change `rimu` to the user that piefed runs as.

Once a week or so it's good to run `remove_orphan_files.sh` to save disk space:

```
5 4 * * 1 rimu cd /home/rimu/pyfedi && /home/rimu/pyfedi/remove_orphan_files.sh
```

One per day there are some maintenance tasks that PieFed needs to do:

```
5 2 * * * rimu cd /home/rimu/pyfedi && /home/rimu/pyfedi/daily.sh
```

If celery is hanging occasionally, put this script in /etc/cron.hourly:

```
#!/bin/bash

# Define the service to restart
SERVICE="celery.service"

# Get the load average for the last 1 minute
LOAD=$(awk '{print $1}' /proc/loadavg)

# Check if the load average is less than 0.1
if (( $(echo "$LOAD < 0.1" | bc -l) )); then
    # Restart the service
    systemctl restart $SERVICE
    # Log the action
    echo "$(date): Load average is $LOAD. Restarted $SERVICE." >> /var/log/restart_service.log
else
    # Log that no action was taken
    echo "$(date): Load average is $LOAD. No action taken." >> /var/log/restart_service.log
fi

```

Adjust the echo "$LOAD < 0.1" to suit your system.

### Email

Email can be sent either through SMTP or Amazon web services (SES). SES is faster but PieFed does not send much
email so it probably doesn't matter which method you choose.

#### AWS SES

PieFed uses Amazon's `boto3` module to connect to SES. Boto3 needs to log into AWS and that can be set up using a file
at `~/.aws/credentials` or environment variables. Details at https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html.

In your `.env` you need to set the AWS region you're using for SES. Something like `AWS_REGION = 'ap-southeast-2'`.

#### CDN

A CDN like Cloudflare is recommended for instances with more than a handful of users. [Recommended caching settings](https://join.piefed.social/2024/02/20/how-much-difference-does-a-cdn-make-to-a-fediverse-instance/).

Some Cloudflare tips:

- Ensure you exclude the URL "/inbox" from the Cloudflare WAF [as shown here](https://join.piefed.social/wp-content/uploads/2024/10/disable-waf-on-inbox.png). If you don't do this there will be federation issues.
- Under Speed -> Optimization -> Content Optimization, turn everything off especially "Rocket Loader" to avoid JavaScript problems.
- Paying careful attention to the caching settings can sharply reduce the load on your server - see [these Cloudflare caching tips](https://join.piefed.social/2024/02/20/how-much-difference-does-a-cdn-make-to-a-fediverse-instance/).

PieFed has the capability to automatically remove file copies from the Cloudflare cache whenever
 those files are deleted from the server. To enable this, set these variables in your `.env` file:

- `CLOUDFLARE_API_TOKEN` - go to https://dash.cloudflare.com/profile/api-tokens and create a "Zone.Cache Purge" token.
- `CLOUDFLARE_ZONE_ID` - this can be found in the right hand column of your Cloudflare dashboard in the API section.

#### SMTP

To use SMTP you need to set all the `MAIL_*` environment variables in you `.env` file. See `env.sample` for a list of them.

#### Testing email

You need to set `MAIL_FROM` in `.env` to some email address.

Log into Piefed then go to https://yourdomain/test_email to trigger a test email. It will use SES or SMTP depending on
which environment variables you defined in .env. If `MAIL_SERVER` is empty it will try SES. Then if `AWS_REGION` is empty it'll
silently do nothing.

---


<div id="pre-requisites-for-mac-os"></div>

## Pre-requisites for Mac OS

#### Install Python Version Manager (pyenv)
see this site: https://opensource.com/article/19/5/python-3-default-mac
    
```bash
brew install pyenv
```

#### Install Python3 version and set as default (with pyenv) 

```bash
pyenv install 3.8.6
pyenv global 3.7.3
```

Note..
You may see this error when running `pip install -r requirements.txt` in regards to psycopg2:
    
    ld: library not found for -lssl
    clang: error: linker command failed with exit code 1 (use -v to see invocation)
    error: command 'clang' failed with exit status 1

If this happens try installing openssl...
Install openssl with brew install openssl if you don't have it already.
    
`brew install openssl`
    
Add openssl path to LIBRARY_PATH :
    
    export LIBRARY_PATH=$LIBRARY_PATH:/usr/local/opt/openssl/lib/
    
---

<div id="notes-for-windows-wsl2"></div>

## Notes for Windows (WSL 2 - Ubuntu 22.04 LTS - Python 3.9.16)

**Important:**
    Python 3.10+ or 3.11+ may cause some package or compatibility errors. If you are having issues installing packages from
    `requirements.txt`, try using Python 3.8 or 3.9 instead with `pyenv` (https://github.com/pyenv/pyenv).
    Follow all the setup instructions in the pyenv documentation and setup any version of either Python 3.8 or 3.9.
    If you are getting installation errors or missing packages with pyenv, run 

```bash
sudo apt-update
sudo apt install build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev curl libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev llvm
```
---

#### Install Python 3, pip, and venv

```bash
sudo apt-get update
sudo apt-get upgrade
sudo apt-get install python3 python3-pip ipython3 libpq-dev python3-psycopg2 python3-dev build-essential redis-server
sudo apt-get install python3-venv
```

#### Setup venv first before installing other packages
**Note: **
    (Replace <3.9> with your version number if you are using another version of Python, 
    e.g. 'sudo apt-get install python3.10-venv' for Python 3.10. Repeat for the rest of the instructions below.)

```bash
python3.9 -m venv ./venv
source venv/bin/activate
```

Make sure that your venv is also running the correct version of pyenv. You may need to re-setup venv if you setup venv before pyenv.

Follow the package installation instructions above to get the packages

```bash
python3.9 -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

<div id="notes-for-pip-package-management"></div>

---

## Notes for Pip Package Management:

make sure you have `wheel` installed:
```bash
pip install wheel
```
    
install packages from a file:
```bash
pip install -r requirements.txt
```

dump currently installed packages to file:
```bash
pip freeze > requirements.txt
```

upgrade a package:
```bash
pip install --upgrade <package_name>
```

