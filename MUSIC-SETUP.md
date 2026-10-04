# Music add-on setup

The music add-on uses yt-dlp, Deno/EJS, the bgutil PO Token provider and a
dedicated YouTube account cookie export. On this VPS, anonymous YouTube
requests are rejected with LOGIN_REQUIRED, while authenticated requests using
the cookie file work.

## 1. Install/update Python packages

```bash
cd ~/itc-notif
source venv/bin/activate
python -m pip install -U -r requirements-club.txt
```

## 2. Keep the PO Token provider running

```bash
docker run -d \
  --name bgutil-provider \
  --restart unless-stopped \
  --init \
  -p 127.0.0.1:4416:4416 \
  brainicism/bgutil-ytdlp-pot-provider
```

If it already exists:

```bash
docker start bgutil-provider
```

Check it:

```bash
docker ps --filter name=bgutil-provider
curl http://127.0.0.1:4416/ping
```

## 3. YouTube cookies

Place the Netscape-format cookie export at:

```text
/home/krum/itc-notif/youtube-cookies.txt
```

Protect it:

```bash
chmod 600 /home/krum/itc-notif/youtube-cookies.txt
```

The file is ignored by git and must never be committed.

The default path can be overridden in .env:

```dotenv
MUSIC_YTDLP_COOKIES=/home/krum/itc-notif/youtube-cookies.txt
MUSIC_POT_PROVIDER_URL=http://127.0.0.1:4416
```

## 4. Verify YouTube manually

```bash
cd ~/itc-notif
source venv/bin/activate
yt-dlp -v \
  --cookies /home/krum/itc-notif/youtube-cookies.txt \
  "https://www.youtube.com/watch?v=JZC4RHVdiWA"
```

Successful output should contain:

```text
Found YouTube account cookies
```

and should proceed to select/download media formats instead of returning
LOGIN_REQUIRED.

## 5. Restart the bot

Restart the systemd service that runs run_club.py, then follow its log.

```bash
sudo systemctl restart YOUR-SERVICE-NAME
sudo journalctl -u YOUR-SERVICE-NAME -f
```

Then test in Discord:

```text
/music play https://www.youtube.com/watch?v=JZC4RHVdiWA
```

or:

```text
/music play Avicii Wake Me Up
```

## Security

Treat youtube-cookies.txt like a password/session token. Do not paste it into
chat, commit it to GitHub, or share it. Use a dedicated Google/YouTube account
for the bot because automated access can cause the account to be challenged or
restricted.
