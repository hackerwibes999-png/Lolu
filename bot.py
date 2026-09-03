import os
import zipfile
import shutil
import tempfile
import logging
import json
import re
import time
from pathlib import Path
from datetime import datetime
from github import Github, GithubException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")          # "username/tor-sites"
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN")  # Optional, for auto-fetch .onion
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID")  # Optional
# ======================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_repo():
    return Github(GITHUB_TOKEN).get_repo(GITHUB_REPO)

def read_metadata(repo):
    try:
        contents = repo.get_contents("sites.json")
        data = json.loads(contents.decoded_content.decode())
        return data, contents.sha
    except:
        return {}, None

def write_metadata(repo, data, sha=None):
    content = json.dumps(data, indent=2)
    if sha:
        repo.update_file("sites.json", "Update metadata", content, sha)
    else:
        repo.create_file("sites.json", "Create metadata", content)

def extract_zip(zip_path: Path, extract_to: Path):
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_to)

def push_website_to_main(repo, site_root: Path, site_name: str):
    """
    Overwrites /var/www/mysite in the main branch with user's files
    and commits directly. This triggers Railway auto-deploy.
    """
    main_ref = repo.get_branch("main")
    main_sha = main_ref.commit.sha

    # We will update all files in var/www/mysite
    target_folder = "var/www/mysite"

    # First, delete all existing files in that folder (to remove old files)
    try:
        contents = repo.get_contents(target_folder, ref="main")
        for content in contents:
            if content.type == "file":
                repo.delete_file(content.path, f"Removing old file", content.sha, branch="main")
    except GithubException as e:
        if e.status != 404:
            raise

    # Now upload new files
    for user_file in site_root.rglob("*"):
        if user_file.is_file():
            rel = user_file.relative_to(site_root)
            github_path = f"{target_folder}/{rel}".replace("\\", "/")
            with open(user_file, 'rb') as f:
                content = f.read()
            try:
                repo.create_file(github_path, f"Add {rel} for {site_name}", content, branch="main")
            except GithubException:
                # If it exists (shouldn't, we deleted), update it
                file_info = repo.get_contents(github_path, ref="main")
                repo.update_file(github_path, f"Update {rel} for {site_name}", content, file_info.sha, branch="main")

    return True

def get_railway_onion(service_id):
    """Fetch the latest deployment logs and extract .onion address."""
    if not RAILWAY_API_TOKEN or not service_id:
        return None
    url = "https://api.railway.app/v2/deployments"
    headers = {"Authorization": f"Bearer {RAILWAY_API_TOKEN}"}
    params = {"serviceId": service_id, "limit": 1}
    try:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data:
            dep_id = data[0]["id"]
            log_url = f"https://api.railway.app/v2/deployments/{dep_id}/logs"
            log_resp = requests.get(log_url, headers=headers)
            log_resp.raise_for_status()
            match = re.search(r"Generated onion address:\s*([a-z0-9]+\.onion)", log_resp.text)
            if match:
                return match.group(1)
        return None
    except Exception as e:
        logger.error(f"Railway API error: {e}")
        return None

# ================== BOT HANDLERS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Tor Deployer Bot**\n\n"
        "Send a `.zip` file with your website (HTML/CSS/JS).\n"
        "It will be deployed as a `.onion` site on Railway.\n\n"
        "Commands:\n"
        "/list – show deployed sites history\n"
        "/info <name> – show site details\n"
        "/delete <name> – remove from history (not live site)",
        parse_mode="Markdown"
    )

async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo = get_repo()
    metadata, _ = read_metadata(repo)
    if not metadata:
        await update.message.reply_text("No sites deployed yet.")
        return
    msg = "📋 **Deployed Sites:**\n" + "\n".join(f"• {s}" for s in metadata.keys())
    await update.message.reply_text(msg, parse_mode="Markdown")

async def info_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /info <site_name>")
        return
    name = context.args[0]
    repo = get_repo()
    metadata, _ = read_metadata(repo)
    site = metadata.get(name)
    if not site:
        await update.message.reply_text(f"Site '{name}' not found.")
        return
    msg = f"📄 **Site:** {name}\n"
    msg += f"🌐 **Onion:** {site.get('onion', 'Unknown')}\n"
    msg += f"📅 **Created:** {site.get('created', 'Unknown')}\n"
    msg += f"🔄 **Updated:** {site.get('updated', 'Unknown')}"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def delete_site(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /delete <site_name>")
        return
    name = context.args[0]
    repo = get_repo()
    metadata, sha = read_metadata(repo)
    if name not in metadata:
        await update.message.reply_text(f"Site '{name}' not found.")
        return
    del metadata[name]
    write_metadata(repo, metadata, sha)
    await update.message.reply_text(f"✅ Site '{name}' removed from history.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith('.zip'):
        await update.message.reply_text("❌ Please send a .zip file.")
        return

    site_name = doc.file_name.replace('.zip', '').strip()
    if not site_name:
        await update.message.reply_text("Invalid filename.")
        return

    await update.message.reply_text(f"⏳ Deploying '{site_name}'...")

    # Download zip
    file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        zip_path = Path(tmp.name)

    extract_dir = Path(tempfile.mkdtemp())
    try:
        extract_zip(zip_path, extract_dir)
        items = list(extract_dir.iterdir())
        site_root = items[0] if len(items) == 1 and items[0].is_dir() else extract_dir

        repo = get_repo()
        push_website_to_main(repo, site_root, site_name)

        # Update metadata
        metadata, sha = read_metadata(repo)
        if site_name not in metadata:
            metadata[site_name] = {}
        metadata[site_name]['created'] = metadata[site_name].get('created', datetime.now().isoformat())
        metadata[site_name]['updated'] = datetime.now().isoformat()
        metadata[site_name]['filename'] = doc.file_name

        # Try to fetch .onion from Railway
        await update.message.reply_text("✅ Files pushed! Waiting for Railway to build...")
        onion = None
        if RAILWAY_API_TOKEN and RAILWAY_SERVICE_ID:
            await update.message.reply_text("⏳ Fetching .onion from Railway logs (takes ~1 min)...")
            for _ in range(6):  # try for ~2 minutes
                time.sleep(20)
                onion = get_railway_onion(RAILWAY_SERVICE_ID)
                if onion:
                    break
        if onion:
            metadata[site_name]['onion'] = onion
            await update.message.reply_text(f"🌐 **Live at:** `{onion}`", parse_mode="Markdown")
        else:
            await update.message.reply_text("⚠️ Check Railway logs manually for the .onion address.")

        write_metadata(repo, metadata, sha)
        await update.message.reply_text(f"✅ Site '{site_name}' is live!")

    except Exception as e:
        logger.exception("Deploy failed")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_sites))
    app.add_handler(CommandHandler("info", info_site))
    app.add_handler(CommandHandler("delete", delete_site))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    logger.info("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()
