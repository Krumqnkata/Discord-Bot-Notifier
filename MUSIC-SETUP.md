# Music add-on: YouTube PO Token setup

The music add-on uses yt-dlp plus the bgutil PO Token provider so the bot can
play YouTube audio from a VPS without using a personal Google account or
exported browser cookies.

## 1. Install/update Python packages

```bash
cd ~/itc-notif
source venv/bin/activate
python -m pip install -U -r requirements-club.txt
```

## 2. Start the PO Token provider

The simplest option is Docker. It listens only on localhost:

```bash
docker run -d \
  --name bgutil-provider \
  --restart unless-stopped \
  --init \
  -p 127.0.0.1:4416:4416 \
  brainicism/bgutil-ytdlp-pot-provider
```

Check it:

```bash
docker ps --filter name=bgutil-provider
docker logs --tail 50 bgutil-provider
```

The bot defaults to:

```text
http://127.0.0.1:4416
```

To use another provider URL, add this to .env:

```dotenv
MUSIC_POT_PROVIDER_URL=http://127.0.0.1:4416
```

## 3. Verify yt-dlp sees the provider

```bash
source ~/itc-notif/venv/bin/activate
yt-dlp -v "https://www.youtube.com/watch?v=JZC4RHVdiWA"
```

The verbose output should list a bgutil PO Token provider.

## 4. Restart the existing bot service

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

## Notes

- No Google login or cookies are required by this setup.
- The provider can help with YouTube bot checks and 403 responses, but it cannot
  guarantee that an aggressively blocked datacenter IP will always work.
- Keep the provider bound to 127.0.0.1 unless you have a specific reason to
  expose it.
