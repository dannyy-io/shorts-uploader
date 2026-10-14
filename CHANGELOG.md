# What's new

## 1.10.0
- 🪝 Hook text: big text on the video (e.g. "wait for his reaction") to make people keep watching. Two looks, top or middle, whole video or the first 3/5 seconds, with a live preview.
- ✨ The AI writes a hook for each video: "Use as hook" on the Upload tab, and "Put AI hook text on each video" in Bulk add / Generate & queue.
- The AI now adds fitting emojis to titles and captions when they suit the video's vibe.
- Videos summarized before this update are summarized again once, to get a hook and emojis.

## 1.9.1
- When Gemini is overloaded ("503 UNAVAILABLE / high demand"), the app now waits and retries, then switches to another Gemini model instead of failing.
- Generate & queue: a video that still hits a busy Gemini goes back in line and is tried again 1, 2 and 3 minutes later before it counts as failed.

## 1.9.0
- ✨ Generate & queue: in Bulk add, one click has the AI write every video's title, caption and tags, then adds each one to the queue. It runs in the background, so you can close the window; progress shows at the top of the Download tab, with Stop and Retry.
- Each video's AI caption goes at the start of its description by default, followed by your "Description for all".
- Downloads show "Writing title…" while the AI is on them.
- The app won't restart for an update while a batch is running.

## 1.8.0
- Downloads now show where each video stands: Not queued, Queued (with its post time), Posted or Skipped. Queueing a video and posting it update this by themselves, and videos you'd already queued or posted are picked up too.
- Filter the Downloads list by status, e.g. "Not queued" to see what's left to queue. "Select all" picks everything in the current view.
- Tick videos to Mark posted, Skip, or Clear mark.
- Downloads list shows all your videos, not just the newest 40.

## 1.7.1
- AI summaries work with Google's new "AQ." API keys: the app tries each way Google accepts a key and remembers the one that works.
- When Gemini rejects a key, Settings now shows Google's exact reason, plus what to do about it.

## 1.7.0
- ✨ AI summaries: Gemini watches the video and suggests a summary, 3 titles, a description and tags (post-specific, niche and broad). Set up in Settings → AI summaries with a free key.
- New Tags field: sent to YouTube's tags; optionally added as hashtags on every platform.
- Bulk add: shared description and tags, and "Generate titles & tags" for every video.
- Download tab: paste several links at once, then queue them together; tick downloads and "Queue selected".
- Videos dragged in from your PC are only sent once, even if you summarize them first.

## 1.6.1
- Fix: a fresh install could fail to start with "No module named 'requests'". The launcher now installs the app's packages first, and checks they actually load.
- Stopping the server in Dockge is now instant (it used to hang for 10 seconds).

## 1.6.0
- Runs on a home server (Docker/Dockge) so scheduled posts go out with your PC off. Password-protected; open it from any device on your network.
- Settings → Move to another computer: export your accounts, presets, post times, history and queue to one file and import it elsewhere (optionally moving the queue so nothing posts twice).
- Connecting YouTube/TikTok on the server: paste the address you land on after signing in.
- Server downloads can use an uploaded cookies.txt for sites that need a login.

## 1.5.0
- Several post times per day (e.g. 12 AM and 12 PM): set them in the Queue tab. "Next open slot" fills them in order.
- "What's new" after an update now shows the notes for the version you got.

## 1.4.1
- More reliable update checks (falls back to GitHub's API if needed).

## 1.4.0
- Installs like a real app: Start Menu and desktop icon, opens in its own window.
- Updates itself automatically from GitHub, and rolls back if an update won't start.
- Tray icon keeps it running for scheduled posts (Open, Check for updates, Quit).
- Settings → App: version, check for updates, auto-update switch, Quit.

## 1.3.0
- Auto-fix format (vertical 1080×1920 MP4, blurred background or crop, trim to 3:00).
- Bulk add to the queue, per-platform captions, caption presets with {creator}/{link}/{title}.
- Instagram cover frame picker. Downloads remember the original creator and link.

## 1.2.0
- Scheduling: post at a set time or one per day, Queue tab, Start with Windows.

## 1.1.0
- Download tab: save videos from YouTube, Instagram, TikTok links.

## 1.0.0
- Upload one video to YouTube Shorts, Instagram Reels and TikTok at once.
