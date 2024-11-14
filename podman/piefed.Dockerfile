FROM python:3.13-alpine3.20

ENV USERNAME=piefed
ENV HOME="/home/$USERNAME"
ENV PATH="$HOME/.local/bin:$PATH"

# System packages needed for piefed
RUN apk add shadow ffmpeg postgresql-client

RUN groupadd -r $USERNAME -g 1000 && useradd -u 1000 -r -g $USERNAME -m -d $HOME -s /sbin/nologin -c "piefed user" $USERNAME && chmod 755 $HOME

ARG UID=1000
ARG GID=1000
USER $USERNAME

WORKDIR $HOME/src
