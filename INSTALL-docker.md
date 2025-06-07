### Easy Way: Docker

#### PRE-INSTALL REQUIREMENTS
- Registered domain name for external access
- Require SSL connection (Nginx/Caddy reverse proxy, cloudflare zero trust tunnel, tailscale, ngrok, etc.)

Note that SSL is not a hard requirement for local testing or development. By including `HTTP_PROTOCOL='http'` in the `.env.docker` file, some features will play a little nicer without SSL. This is not a teribly well-vetted workflow, so there might still be cases in which SSL is expected/required (such as S3 usage). This is primarily intended to ease development of basic features or UI changes.


#### COPY GITHUB REPOSITORY INTO A NEW DIRECTORY (PYFEDI/)
```bash
sudo git clone https://codeberg.org/rimu/pyfedi.git
cd pyfedi/
```

#### PREPARE DOCKER ENVIRONMENT FILE
```bash
sudo cp env.docker.sample .env.docker
sudo nano .env.docker
```
- `SECRET_KEY='...'` - Replace text with random numbers and letters
- `SERVER_NAME='...'` - Enter a registered domain name (do NOT include `http://`). Use the address `127.0.0.1:8030` for testing or development. If your testing/dev instance needs
to federate with other instances then you will need a domain name. ngrok.com has a free tier you can use - get this before proceeding because changing SERVER_NAME later
involves wiping all data and starting again.
- Add additional variables (Mail, Cloudflare, etc.) relevant to your needs. See file `pyfedi/env.sample` for example variables.


#### CHECK COMPOSE.YML
- Check ports (8030:5000) and volume definitions if this is relevant to your needs. If not, skip this step.
```bash
sudo nano compose.yaml
```

#### CREATE REQUIRED FOLDERS WITH REQUIRED PERMISSIONS
- Replace `<USERNAME>` with account username
```bash
sudo mkdir pgdata
sudo chown -R <USERNAME>:<USERNAME> ./pgdata
sudo mkdir media
sudo chown -R <USERNAME>:<USERNAME> ./media
sudo mkdir logs
sudo chown -R <USERNAME>:<USERNAME> ./logs
sudo mkdir tmp
sudo chown -R <USERNAME>:<USERNAME> ./tmp
```

#### BUILD PIEFED
```bash
export DOCKER_BUILDKIT=1
sudo docker compose up --build
```
- Wait until text in terminal stops scrolling. Ignore the configuration check errors at this point.
- Test external access from browser (On port 8030). Watch for movement in terminal window. Browser will show "Internal Server Error" message. Proceed to initialize database to address this error message.

#### INITIALIZE DATABASE
- Open a new terminal window
```bash
sudo docker exec -it piefed_app1 sh
```
```bash
export FLASK_APP=pyfedi.py
flask init-db
```
- Enter username, email (optional), and password.
- Test external access from browser again. PieFed should now load. Login as admin with the same username and password.
```bash
exit
```
- Close this terminal window

#### SHUT DOWN PIEFED & START AGAIN IN BACKGROUND
- Return to main terminal window and press `CTRL+C` to shut down PieFed
```bash
sudo docker compose up -d
```

- During startup, you might see "Running configuration check..." followed by validation messages. Checkmarks (✅)
indicate successful configuration, warnings (⚠️) are usually fine to ignore, but X marks (❌) indicate critical issues that need fixing.

#### SETUP CRON (AUTOMATED) JOBS
```bash
sudo nano /etc/cron.d/piefed
```
- Copy & Paste the text below
- Replace `<USERNAME>` with account username
```
5 2 * * * <USERNAME> docker exec piefed_app1 bash -c "cd /app && ./daily.sh"
5 4 * * 1 <USERNAME> docker exec piefed_app1 bash -c "cd /app && ./remove_orphan_files.sh"
1 */6 * * * <USERNAME> docker exec piefed_app1 bash -c "cd /app && ./email_notifs.sh"
*/5 * * * * <USERNAME> docker exec piefed_app1 bash -c "cd /app && ./send_queue.sh"
```

#### UPDATING & RESTARTING PIEFED
```bash
cd pyfedi/
sudo git pull
sudo docker compose up --build
```
- `CTRL+C` to shut down PieFed
```bash
sudo docker compose up -d
```