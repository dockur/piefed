# Contributing to PieFed

When it matures enough, PieFed will aim to work in a way consistent with the [Collective Code Construction Contract](https://42ity.org/c4.html).

Please discuss your ideas in an issue at https://codeberg.org/rimu/pyfedi/issues before 
starting any large pieces of work to ensure alignment with the roadmap, architecture and processes.

Check out [the Matrix chatroom](https://matrix.to/#/#piefed-community:matrix.org).

The general style and philosophy behind the way things have been constructed is well described by 
[The Grug Brained Developer](https://grugbrain.dev/). If that page resonates with you then you'll
probably enjoy your time here! The codebase needs to be simple enough that new developers of all
skill levels can easily understand what's going on and onboard quickly without a lot of upfront 
time investment. Sometimes this will mean writing slightly more verbose/boring code or avoiding the
use of advanced design patterns.

Mailing list, Matrix channel, etc still to come.

# Technology stack

- Python 
- Flask
- Jinja
- SCSS/SASS
- SQL - Postgresql
- Redis

Python developers with no Flask experience can quickly learn Flask by exploring the 
[Flask Mega Tutorial](https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-i-hello-world-2018)
which will guide them through the process of building a simple social media app. Django is
very similar to Flask so developers familiar with that framework will have an easier
time of things.

# Coding Standards / Guidelines

**[PEP 8](https://peps.python.org/pep-0008/)** covers the basics. PyCharm encourages this by default - 
VS Code coders are encouraged to try the free community edition of PyCharm but it is by no means required. 

Use PEP 8 conventions for line length, naming, indentation. Use descriptive commit messages.

Database model classes are singular. As in "Car", not "Cars".

### Directory structure

Where possible, the structure should match the URL structure of the site. e.g. "domain.com/admin" 
should be in a directory called "admin". Exceptions to this are '/activitypub' which contains
modules for server-to-server communication and 'main' which is all the public non-authenticated
parts of the app.

Most of the interesting code is in the /app directory. Within that are:

 - /templates which contains all the Jinja2 code for rendering HTML.
 - /static is all the images, CSS and JS files. SCSS files are compiled into CSS.
 - /utils.py contains misc helpful functions. Within each directory is often another utils.py for
helpful functions that pertain to modules in that directory only.
 - /models.py is the database interface. Each class in this file corresponds to a table in the database. 
Changes to this file are turned into changes in the DB by using '[migrations](https://www.onlinetutorialspoint.com/flask/flask-how-to-upgrade-or-downgrade-database-migrations.html)'.
 - /community/* pertains to viewing, posting within and managing communities.

# Code of conduct

## Our Pledge

We, the contributors and maintainers of the PieFed project, pledge to create a welcoming and harassment-free environment for everyone, regardless of age, body size, disability, ethnicity, gender identity and expression, level of experience, nationality, personal appearance, race, religion, or sexual identity and orientation.

## Our Standards

Examples of behavior that contributes to creating a positive environment include:

- **Being respectful**: Treat all individuals with respect and kindness, and consider their perspectives and experiences valuable.

- **Being inclusive**: Welcome people of all backgrounds and identities, and foster an environment where everyone feels comfortable to participate.

- **Being empathetic**: Be understanding and empathetic toward others, especially when they make mistakes or ask for help.

- **Being constructive**: Provide constructive feedback and engage in discussions that promote the growth of the community and its members.

- **Being collaborative**: Work together with others in the spirit of cooperation and teamwork, striving for the best possible outcomes for the project.

- **Being accountable**: Take responsibility for your actions and their impact on others, and learn from your mistakes.

Examples of unacceptable behavior include:

- **Harassment and discrimination**: Engaging in any form of harassment, discrimination, or unwelcome behavior based on the characteristics listed above.

- **Intimidation or threats**: Using intimidating language or making threats toward others.

- **Personal attacks**: Engaging in personal attacks, insults, or trolling.

- **Unwanted attention**: Persistently and aggressively pursuing or targeting someone against their will.

- **Unconstructive criticism**: Providing feedback that is not constructive or respectful.

## Enforcement Responsibilities

Project maintainers are responsible for enforcing these standards fairly and consistently. They have the authority and responsibility to address any inappropriate behavior, and they may take appropriate actions in response to violations. These actions may include warning, moderation, or temporary or permanent expulsion from the project community.

## Reporting Violations

If you experience or witness any behavior that violates this code of conduct, please report it to the project maintainers. All reports will be kept confidential.

## Attribution

This Code of Conduct is adapted from the [Contributor Covenant](https://www.contributor-covenant.org/version/2/0/code_of_conduct.html).

## Conclusion

We all share the responsibility of upholding these standards. Let's work together to create a welcoming and inclusive environment that fosters collaboration, learning, and the growth of the PieFed community.
