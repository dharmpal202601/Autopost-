# WA Channel Auto Publisher 🚀

A production-ready Windows desktop automation tool that monitors a selected WhatsApp Channel and automatically publishes new image posts (with their captions) to a Facebook Page and Instagram Business account.

---

## Features

- **Automated WhatsApp Channel Monitoring**: Automatically checks a public WhatsApp Channel for new posts via headed Playwright browser automation (Chromium).
- **Meta Graph API Integration**: Uses official, secure Meta Graph API v25.0 endpoints to publish directly to Facebook Pages and Instagram Business accounts.
- **Persistent Publish Queue**: All pending and processing posts are persisted locally in a SQLite database (with WAL mode enabled) to handle crash recovery and restarts.
- **Secure Token Storage**: Encrypts sensitive credentials (like Page tokens and app secrets) using Fernet AES-256 with the master key stored in the **Windows Credential Manager** via keyring.
- **Real-Time Control Dashboard**: A polished Flask + Socket.IO dashboard (`http://localhost:5000`) showing account statuses, live queue management, historical logs, and settings.
- **Task Scheduler Registration**: Auto-starts seamlessly on Windows login as a background process (`pythonw.exe`).
- **Flexible Image Hosting**: Automatically generates public URLs required by the Instagram API using local hosting exposed via `ngrok` (with a fallback to `imgbb`).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Windows Task Scheduler (runs on user logon)                     │
│  └── main.py (via pythonw.exe)                                  │
│       ├── Thread 1: Playwright WhatsApp Web monitor             │
│       ├── Thread 2: Queue worker → Meta API publisher           │
│       └── Thread 3: Flask + Socket.IO dashboard (port 5000)     │
├─────────────────────────────────────────────────────────────────┤
│  SQLite DB (WAL mode) — shared by all threads                   │
│  ├── posts table    (queue + history + duplicate tracking)      │
│  ├── logs table     (streamed to dashboard in real-time)        │
│  └── config table   (runtime settings)                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Installation

### Prerequisites
- Windows 10 / 11
- Python 3.10+ (ensure "Add Python to PATH" is checked during installation)
- Meta Developer Account (to create app and obtain page access tokens)

### Manual Setup
1. Clone or copy the project files to a local directory (e.g. `C:\WA_Publisher`).
2. Open a terminal in the project directory and install the requirements:
   ```cmd
   pip install -r requirements.txt
   ```
3. Run the interactive setup wizard:
   ```cmd
   python setup_wizard.py
   ```
   *The setup wizard will install Playwright Chromium, configure your WhatsApp Channel, collect your Meta API tokens, verify accounts, and register the Windows Startup Task.*

---

## Meta Application Configuration

To publish to Facebook and Instagram, you must create a Meta Developer App.

### Step 1: Create a Meta App
1. Go to [Meta Developers Portal](https://developers.facebook.com/) and register as a developer.
2. Click **Create App**, select **Other**, then select **Business** as the app type.
3. Name your app (e.g., `WA Auto Publisher`) and complete the creation process.

### Step 2: Set Up Products
1. In the App Dashboard, add **Facebook Login for Business** and **Instagram Graph API**.

### Step 3: Required Scopes and Permissions
Ensure your system uses the following scopes (which do not require App Review for your own page/account):
- `pages_manage_posts`
- `pages_read_engagement`
- `pages_show_list`
- `instagram_business_basic`
- `instagram_business_content_publish`

### Step 4: Long-Lived Page Access Token Walkthrough
By default, the Graph Explorer provides a token that expires in 1–2 hours. Follow these steps to generate a **permanent/never-expiring** Page access token:

1. **Get a short-lived User Access Token**:
   - Go to [Graph API Explorer](https://developers.facebook.com/tools/explorer/).
   - Select your app and add the required scopes.
   - Click **Generate Access Token** and log in.

2. **Convert to a long-lived User Token** (lasts 60 days):
   - Make a `GET` request to the exchange endpoint:
     ```
     GET https://graph.facebook.com/v25.0/oauth/access_token?
         grant_type=fb_exchange_token&
         client_id={your-app-id}&
         client_secret={your-app-secret}&
         fb_exchange_token={short-lived-user-token}
     ```
   - Copy the `access_token` from the response.

3. **Get a permanent Page Access Token**:
   - Using the 60-day token, query your page accounts:
     ```
     GET https://graph.facebook.com/v25.0/me/accounts?access_token={60-day-user-token}
     ```
   - Find your target page in the data list and copy its `access_token`. This token **never expires** unless you change your password or revoke permissions.

---

## Usage Guide

### First-Run Setup Wizard
The setup wizard (`setup_wizard.py` or double-clicking `run_setup.bat`) guides you through 11 steps:
1. Verify Python 3.10+
2. Install Python dependencies
3. Install Playwright Chromium
4. Configure WhatsApp Channel URL
5. Configure Meta Developer details (IDs, encrypted tokens)
6. Verify access tokens and permissions
7. Set publishing delay & poll intervals
8. Test encrypting and decrypting configuration
9. Open browser for WhatsApp QR login (needed to save browser session details)
10. Register Windows Startup Task Scheduler
11. Launch the local control dashboard

### Control Dashboard
Run the dashboard directly or use the `scripts/start_dashboard.bat` shortcut.
- **API Status Panel**: See whether automation is running, if Facebook and Instagram are verified, and your daily Instagram publishing quota (limit 25 posts/day).
- **Active Queue**: Manage pending posts. You can view image thumbnails, edit captions, and check retry statuses.
- **Log Streamer**: View real-time log records (color-coded for Info, Warning, Error, and Success) as the monitor scans and publishes.
- **Publish History**: View past successful posts, including Facebook/Instagram post IDs.
- **Configuration Settings**: Adjust polling intervals, publish delay times, or toggle Facebook/Instagram publishing on the fly.
- **Test Publisher**: Drag and drop a local image file to immediately test publishing to your pages.

---

## Troubleshooting

### WhatsApp Web monitor fails or does not log in
- Ensure you have scanned the QR code. You can run the setup wizard again to launch a headed browser and complete the login.
- If selectors fail, check that your WhatsApp web is working and that you are using the correct Channel link format: `https://whatsapp.com/channel/YOUR_CHANNEL_ID`.

### Instagram publishes fail with "URL could not be fetched"
- The Instagram API requires a public URL. The tool creates a local server and uses `ngrok` or `imgbb` to host the image.
- If using `ngrok`, verify that you have registered a free account and added your authtoken if prompted, or verify that your internet firewall is not blocking incoming ngrok connections.
- If using `imgbb`, verify you have entered a valid imgbb API key in settings/setup.

### Scheduled Task doesn't start
- Double-check that you ran `scripts/install_task.bat` as an Administrator or as the logged-in user.
- Open Windows **Task Scheduler** (`taskschd.msc`) and find the task "WA Auto Publisher". Verify that the action is set to start `pythonw.exe main.py` in your project folder.

---

## License
Private / Proprietary. Built for personal desktop automation.
