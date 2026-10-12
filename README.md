# Shorts Uploader

Drop in a vertical video, type a title and description, tick YouTube / Instagram / TikTok, hit **Upload**. Runs entirely on your PC at `http://127.0.0.1:5005` and talks straight to each platform's official API. Your logins and tokens stay in the `data/` folder on your computer.

## Install (Windows)

1. Install **Python 3.10+** from <https://www.python.org/downloads/>. On the first installer screen, tick **"Add python.exe to PATH"**.
2. Download this repo (green **Code** button → **Download ZIP**), unzip it, and double-click **`Install.bat`**.
3. It installs to `AppData\Local\Programs\Shorts Uploader`, adds **Start Menu and desktop icons**, and opens the app in its own window. If you already had an older copy, run Install.bat from that folder's replacement and it brings your accounts, queue and downloads along.

After that, open it from the Start Menu or desktop icon. Closing the window keeps it running in the **tray** (bottom-right, near the clock) so scheduled posts still go out; right-click the tray icon to **Quit**. Settings → App also has Quit.

**It updates itself.** On start and every few hours it checks this GitHub repo; when there's a newer version it installs it as soon as nothing is uploading, restarts in a few seconds, and shows what's new. If an update ever fails to start, it automatically goes back to the previous version. Turn this off in Settings → App. To remove the app, run `Uninstall.bat` in its folder.

Mac/Linux: run `./start.sh` instead (no installer, but updates still work).

The first time, Settings opens on its own. Each platform needs a one-time "developer app" (free) so the API will talk to you — steps below. You can set up just one platform and add the others later.

> **Read this first — the honest limits.** All three platforms let personal tools upload, but each restricts *unreviewed* apps:
>
> | | Works right away | Needs the platform's app review for |
> |---|---|---|
> | **Instagram** | Public Reels on your own account | Nothing, for your own account |
> | **YouTube** | Uploads, but **locked to Private** | Public/unlisted uploads (free YouTube API audit) |
> | **TikTok** | **Drafts mode**: video lands in your TikTok inbox, you tap Post in the app | Posting publicly straight from the tool |
>
> So on day one: Instagram is fully hands-off, TikTok is one tap in the app, and YouTube needs the audit approved before videos can go public. The steps for each are below.

---

## Run it on a home server (posts even when your PC is off)

Scheduled posts only go out while the app is running. To post at 12 AM with your PC off, run it on an always-on machine with Docker (a home server with **Dockge** is ideal).

1. **Create the stack.** In Dockge click **+ Compose**, name it `shorts-uploader`, and paste in the contents of [`server/compose.yaml`](server/compose.yaml).
2. **Pick a password.** Change `change-me` next to `SHORTS_UPLOADER_PASSWORD`. If your server isn't in Pacific time, change `TZ` too (post times use it). Click **Deploy**.
3. **Wait for the first start.** It downloads the app and installs what it needs, which takes a few minutes. The log shows `listening on port 5005` when it's ready.
4. **Open it** from any device on your home network: `http://<server-name>:5005` (for example `http://familyhomeserver:5005`), and log in.
5. **Bring your setup over** from the PC app:
   - On the **PC app**, go to Settings → *Move to another computer or the home server*, tick **Move the queue**, and click **Export…**.
   - On the **server**, go to Settings → **Import a backup…** and pick that file. Your accounts, presets, post times, history and queue come across.
   - Then on the PC, quit the app from the tray and turn off *Start with Windows* (Queue tab), so only the server posts.
6. **Connecting accounts on the server** (if you didn't import them): YouTube and TikTok sign-ins finish on an address starting with `http://127.0.0.1:5005/callback`, which won't load because that address means "this device". That's expected. Copy that address and paste it into the box the app shows. If the PC app happens to be running, it shows you the address to copy instead.
7. **Downloads that need a login** (common on Instagram): on the server, use **Upload cookies.txt** in the Download tab instead of "Use my browser login". See *Downloading videos* below.

**Updates** happen on their own, the same as on the PC. Everything lives in the stack folder (`/opt/stacks/shorts-uploader/app`), so redeploying or updating the stack in Dockge keeps your data.

**Keep it private:** don't forward port 5005 on your router. To use it away from home, connect to your home network with a VPN first (WireGuard, for example).

## 1. YouTube

1. Go to <https://console.cloud.google.com/>, create a new project (any name, e.g. "Shorts Uploader").
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **Google Auth Platform** (or "OAuth consent screen") → set it up as **External**, give it an app name and your email.
   - Under **Audience**, add your own Google account as a **test user**.
   - Then click **Publish app** so it's "In production". Without this, Google logs you out every 7 days. (You'll see a "Google hasn't verified this app" screen when connecting — click **Advanced → Go to (your app)**. That's expected for a personal tool.)
4. **Clients → Create client → Application type: Desktop app**. Copy the **Client ID** and **Client secret**.
5. In Shorts Uploader: **Settings → YouTube** → paste both → **Connect YouTube** → sign in with the account that owns your channel.

**Making uploads public:** Google locks videos uploaded through brand-new, unaudited API projects as Private. To lift that, submit the **YouTube API Services audit form**: <https://support.google.com/youtube/contact/yt_api_form>. Describe it as a personal tool that uploads your own videos to your own channel. Until it's approved, uploads will come through as private.

Default quota allows a handful of uploads per day, which is plenty for Shorts.

## 2. Instagram

Your Instagram account must be **Professional** (Creator or Business): Instagram app → Settings → Account type and tools → Switch to professional account. Free, takes a minute.

1. Go to <https://developers.facebook.com/apps> → **Create app**. Pick the option for managing content on Instagram (or **Other → Business** if that's what's offered).
2. In the app dashboard, add the **Instagram** product and open **API setup with Instagram login**.
3. Under **Generate access tokens**, click **Add account**, log into your Instagram, and approve. Then click **Generate token** and copy it.
4. In Shorts Uploader: **Settings → Instagram** → paste the token → **Save & connect**.

You can leave the Meta app in Development mode — it works for your own account because you're the app's admin. Tokens last 60 days; the tool refreshes yours automatically every week as long as you use it at least once every couple of months.

**If uploads fail with a permissions error:** Meta's docs officially describe direct file uploads for the Facebook-Login version of the API. If the Instagram-Login token above gets rejected at the upload step, link your Instagram to a Facebook Page, get a token from the Graph API Explorer with `instagram_basic`, `instagram_content_publish`, `pages_show_list` and `pages_read_engagement`, then in Settings → Instagram → **Advanced** choose **Facebook Login** and enter your Instagram user ID. (That token isn't auto-refreshed — swap in a new one when it expires.)

## 3. TikTok

1. Go to <https://developers.tiktok.com/>, log in, **Manage apps → Connect an app / Create app**.
2. Add the products **Login Kit** and **Content Posting API**. (Turn on **Direct Post** inside Content Posting API if you want that mode.)
3. In **Login Kit**, choose the **Desktop** platform and add this redirect URI exactly:
   `http://127.0.0.1:5005/callback/tiktok`
4. Make sure the scopes **user.info.basic**, **video.upload** and **video.publish** are on.
5. Switch the app to **Sandbox**, create a sandbox, and add your TikTok account as a **target user**. Sandbox has its own client key/secret — copy those.
6. In Shorts Uploader: **Settings → TikTok** → paste key + secret → **Connect TikTok**.

**Modes** (pick per upload):
- **Send to TikTok drafts** (default): the video uploads, TikTok sends you an inbox notification, you open it in the app and hit Post. The tool copies your caption to the clipboard so you can paste it.
- **Post directly**: posts without opening the app, but until TikTok audits your app, posts are forced to "Only me" and your TikTok account must be set to private. Full public posting requires submitting the app for TikTok's review (they ask for a website, privacy policy and a demo video).

---

## Auto-fix format

Under the video preview, **Auto-fix format** (on by default) makes every clip upload-ready: 1080×1920, H.264 video, AAC audio, MP4.

- Files that are already right are uploaded untouched. A .mov or .webm that only needs a different container is repackaged in a second without re-encoding.
- **Horizontal clips:** choose a **blurred background** (the clip sits in the middle with a blurred, zoomed copy behind it) or **crop to fill**.
- **Trim to 3:00** cuts longer videos so YouTube counts them as Shorts. It applies to every platform in that upload.
- Converting a 30–60 second clip takes roughly 10–40 seconds. For scheduled and bulk posts it happens in the background right after you add them, so any problem shows up in the Queue right away instead of at post time.

## Captions: presets, credits and per-platform text

**Presets** (Settings → Caption presets, or "edit presets" next to the dropdown) are text that gets added to the end of a caption: hashtags, credit lines, links. Pick one per upload from the **Add preset** dropdown. They can use placeholders:

- `{creator}`: the original creator's handle
- `{link}`: the link to the original post
- `{title}`: your title

A line is left out entirely if its placeholder is empty. So a preset like

```
🎥 Credit: {creator}
{link}
#anime #edit
```

works both for reposts (all three lines) and your own clips (just the hashtags).

**Creator and link fill in automatically** for videos from the Download tab: the app saves who posted it and where, and shows them under the preview as **Original creator** / **Source link**. You can edit them, or type them in for videos from your PC. Always double-check the handle: sites don't all report it the same way.

**Different caption per platform** (the expandable box under the description) lets you write separate text for each site. On YouTube it replaces the description (the title stays). On Instagram and TikTok it replaces the whole caption. Leave a box empty to use the normal title + description. Your preset is still added to the end of each.

## Instagram cover frame

Play or scrub the preview to the frame you want, pause, and click **Use current frame as cover** in the Instagram card.

## Bulk add

**Bulk add…** (next to Clear) queues many videos at once. Add files from your PC, tick videos from your Downloads, or drop files onto the window. Each one gets its own title and creator, and they're placed on consecutive daily slots. Everything else (platforms, description, preset, per-platform captions, auto-fix settings) comes from the Upload tab, so set those first.

## Scheduling posts (e.g. one a day)

Under the platform toggles on the Upload tab, choose when it goes out:

- **Post now**: uploads right away.
- **Pick a time**: posts at the date and time you choose.
- **Next open slot**: puts the video on the next free post time. Set your post times in the **Queue** tab (for example 12:00 AM and 12:00 PM for two posts a day). Add ten videos this way and you've got five days of posts lined up.

The **Queue** tab lists everything scheduled. You can **Post now**, **Change time**, or remove any of them. If one platform fails (say Instagram's token expired), the post stays in the queue with the error; fix the problem and hit **Retry now**. It only re-posts to the platforms that failed, so you never get a duplicate on the ones that worked.

**Important:** scheduled posts go out *from your PC*, so the app has to be running at post time:

1. In the Queue tab, turn on **Start this app when Windows starts**. After a restart it opens minimized in the taskbar instead of popping up your browser.
2. Keep the black window open (minimizing is fine).
3. Make sure the PC doesn't sleep through post time: Windows Settings → System → Power → *Screen and sleep* → set "put my device to sleep" to **Never** when plugged in (the screen can still turn off).

If the PC was off or asleep when something was due, it posts as soon as the app is running again. Scheduled videos are kept in `data/queue` until they've posted.

## Downloading videos

The **Download** tab (top of the page) saves a video from a link: YouTube, Shorts, Instagram Reels/posts, TikTok and most other video sites. Paste a link and it starts automatically. Files go into the `downloads` folder next to the app; click **Use in uploader** to load one straight into the Upload tab.

- It uses **yt-dlp**, which `start.bat` installs and updates each time you start the app (along with ffmpeg for HD video and Deno, which YouTube needs). If a site suddenly stops working, click **Update downloader** in the Download tab; sites change their pages often and yt-dlp catches up within days.
- Videos are saved as MP4, up to 1080p, in a format all three platforms accept.
- **"Log in" errors (common on Instagram):** set **Use my browser login** to the browser you're signed into that site with. Firefox works most reliably. Chrome/Edge/Brave on Windows lock their cookies while open and use extra encryption, so if that fails, either fully close the browser and retry, or export a cookies file: install a "cookies.txt" export extension, export while on instagram.com or youtube.com, and save it as `data/cookies.txt` in the app folder. It's used automatically when the browser option is Off.
- Keep in mind: re-uploading other people's videos can get you copyright strikes. It's meant for your own videos and clips you have rights to edit.

## Troubleshooting

- **Nothing happens when I click the icon:** it may already be running in the tray. If not, check `data\app.log` in the app folder.
- **Want to see what it's doing:** run `start.bat` in the app folder; it starts with a visible console window.
- **Repair:** run `Install.bat` from the app folder again. It's safe to re-run any time.

## Tips

- **Best format:** MP4, H.264 video + AAC audio, 1080×1920 (9:16), 30 or 60 fps. YouTube only counts it as a Short if it's vertical/square and **3 minutes or less**.
- **Captions:** YouTube gets the title and description separately (title max 100 characters). Instagram and TikTok get one caption: title, blank line, description (max 2,200 characters).
- **Saved hashtags:** set them once in Settings and tick "Add my saved hashtags" to append them to every post.
- Your draft title/description and last-used options are remembered between sessions. **Ctrl + Enter** uploads.
- **Recent uploads** at the bottom keeps links to the last 50.
- Port 5005 taken? Set `SHORTS_UPLOADER_PORT` before starting — and update the TikTok redirect URI to match. YouTube doesn't care about the port.

## Files

```
app.py              local web server + upload jobs
launcher.py         starts the app, installs updates, rolls back bad ones
updater.py          checks GitHub and swaps in new versions
winapp.py           app window, tray icon, Windows pop-ups
install.py          Install.bat / Uninstall.bat
transfer.py         Export / import (move to another computer or the server)
server/             Docker/Dockge stack and its first-start script
downloader.py       the Download tab (yt-dlp)
scheduler.py        the posting queue, background conversion and Start-with-Windows
media.py            auto-fix (ffmpeg)
platforms/          youtube.py, instagram.py, tiktok.py
templates/index.html  the page you use
data/               your settings, tokens, history (created on first run — don't share this folder)
downloads/          videos saved from the Download tab
```
