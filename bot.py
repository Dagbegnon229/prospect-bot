"""
Bot Telegram de Prospection Freelance
Trouve des missions dev web/mobile, genere des candidatures avec l'IA,
et envoie tout pret a copier-coller.
"""

import os
import logging
import asyncio
import json
import re
import html as html_lib
from datetime import datetime
from threading import Thread

import httpx
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PicklePersistence,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
CHAT_ID = os.environ.get("CHAT_ID", "")

KEYWORDS = [
    "frontend", "backend", "fullstack", "full-stack", "full stack",
    "react", "next.js", "nextjs", "vue", "angular", "svelte",
    "node", "django", "flask", "fastapi", "laravel", "php",
    "typescript", "javascript", "python",
    "mobile", "react native", "flutter", "ios", "android",
    "web developer", "web dev", "software engineer", "developer",
    "développeur", "developpeur",
]

PROFIL_DEFAUT = {
    "nom": "Camel",
    "competences": "React, Next.js, React Native, Node.js, TypeScript, Python, Flutter",
    "experience": "Développeur web & mobile freelance avec expérience en création d'applications complètes (frontend + backend + mobile). Spécialisé en React/Next.js et React Native.",
    "portfolio": "https://github.com/Camel",
}

SYSTEM_PROMPT = """Tu es un expert en candidature freelance tech. Tu rédiges des messages de candidature courts, percutants et personnalisés.

Règles :
- Maximum 150 mots
- Ton professionnel mais humain, pas corporate
- Mentionne 2-3 compétences pertinentes pour le poste
- Montre que tu as compris le besoin du client
- Termine par une disponibilité immédiate
- Écris en anglais sauf si le poste est clairement francophone
- Pas de formules bateau comme "I am writing to express my interest"
- Va droit au but"""

GROQ_MODEL = "llama-3.3-70b-versatile"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Health-check server (Koyeb)
# ---------------------------------------------------------------------------
health_app = Flask(__name__)


@health_app.route("/")
def health():
    return "OK", 200


def run_health_server():
    port = int(os.environ.get("PORT", 8000))
    health_app.run(host="0.0.0.0", port=port)


def clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Fetch jobs
# ---------------------------------------------------------------------------
async def fetch_remoteok() -> list[dict]:
    """Fetch dev jobs from RemoteOK API."""
    url = "https://remoteok.com/api"
    headers = {"User-Agent": "ProspectBot/1.0"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        # First element is metadata, skip it
        jobs = []
        for item in data[1:]:
            title = (item.get("position") or "").lower()
            tags = " ".join(item.get("tags") or []).lower()
            company = item.get("company") or ""
            search_text = f"{title} {tags} {company}".lower()
            if any(kw in search_text for kw in KEYWORDS):
                jobs.append({
                    "id": f"rok-{item.get('id', '')}",
                    "title": item.get("position", "N/A"),
                    "company": item.get("company", "N/A"),
                    "url": item.get("url", f"https://remoteok.com/remote-jobs/{item.get('id', '')}"),
                    "description": clean_html((item.get("description") or ""))[:500],
                    "source": "RemoteOK",
                    "date": item.get("date", ""),
                })
        return jobs
    except Exception as e:
        logger.error(f"RemoteOK error: {e}")
        return []


async def fetch_arbeitnow() -> list[dict]:
    """Fetch dev jobs from Arbeitnow API."""
    url = "https://www.arbeitnow.com/api/job-board-api"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        jobs = []
        for item in data.get("data", []):
            title = (item.get("title") or "").lower()
            desc = (item.get("description") or "").lower()
            tags = " ".join(item.get("tags") or []).lower()
            search_text = f"{title} {desc} {tags}"
            if any(kw in search_text for kw in KEYWORDS):
                jobs.append({
                    "id": f"abn-{item.get('slug', '')}",
                    "title": item.get("title", "N/A"),
                    "company": item.get("company_name", "N/A"),
                    "url": item.get("url", ""),
                    "description": clean_html((item.get("description") or ""))[:500],
                    "source": "Arbeitnow",
                    "date": item.get("created_at", ""),
                })
        return jobs
    except Exception as e:
        logger.error(f"Arbeitnow error: {e}")
        return []


async def fetch_jobs() -> list[dict]:
    """Fetch jobs from all sources and deduplicate."""
    results = await asyncio.gather(fetch_remoteok(), fetch_arbeitnow())
    all_jobs = []
    for job_list in results:
        all_jobs.extend(job_list)
    return all_jobs


# ---------------------------------------------------------------------------
# Groq AI - Generate candidature
# ---------------------------------------------------------------------------
async def generate_candidature(job: dict, profil: dict) -> str:
    """Generate a personalized candidature using Groq AI."""
    user_prompt = f"""Poste : {job['title']}
Entreprise : {job['company']}
Description : {job['description']}

Profil du candidat :
- Nom : {profil['nom']}
- Compétences : {profil['competences']}
- Expérience : {profil['experience']}
- Portfolio : {profil['portfolio']}

Rédige un message de candidature court et percutant pour ce poste."""

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            if resp.status_code != 200:
                logger.error(f"Groq HTTP {resp.status_code}: {resp.text}")
                return "⚠️ Erreur lors de la génération de la candidature."
            data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "⚠️ Erreur lors de la génération de la candidature."


# ---------------------------------------------------------------------------
# Format message
# ---------------------------------------------------------------------------
def format_job_message(job: dict, candidature: str) -> str:
    """Format a job + candidature as a Telegram message."""
    return (
        f"🎯 *{escape_md(job['title'])}*\n"
        f"🏢 {escape_md(job['company'])}  •  📡 {escape_md(job['source'])}\n"
        f"🔗 [Voir l'offre]({job['url']})\n"
        f"\n"
        f"✉️ *Candidature prête :*\n"
        f"```\n{candidature}\n```"
    )


def escape_md(text: str) -> str:
    """Escape special MarkdownV2 characters."""
    special = r"_*[]()~`>#+-=|{}.!"
    result = ""
    for ch in text:
        if ch in special:
            result += f"\\{ch}"
        else:
            result += ch
    return result


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------
async def start(update: Update, context) -> None:
    """Main menu with inline buttons."""
    keyboard = [
        [InlineKeyboardButton("🔍 Chercher des missions", callback_data="missions")],
        [InlineKeyboardButton("⚡ Auto-prospection ON/OFF", callback_data="auto")],
        [InlineKeyboardButton("👤 Mon profil", callback_data="profil")],
        [InlineKeyboardButton("❓ Aide", callback_data="aide")],
    ]
    await update.message.reply_text(
        "🚀 *Bot de Prospection Freelance*\n\n"
        "Je trouve des missions dev web/mobile et je génère tes candidatures\\.\n\n"
        "Choisis une action :",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="MarkdownV2",
    )


async def missions_command(update: Update, context) -> None:
    """Fetch missions, generate candidatures, send results."""
    msg = update.message or update.callback_query.message
    chat_id = msg.chat_id

    await context.bot.send_message(chat_id=chat_id, text="🔍 Recherche de missions en cours...")

    # Get profile
    profil = context.bot_data.get("profile", PROFIL_DEFAUT.copy())

    # Get sent jobs set
    sent_jobs: set = context.bot_data.setdefault("sent_jobs", set())

    # Fetch jobs
    jobs = await fetch_jobs()

    # Filter already sent
    new_jobs = [j for j in jobs if j["id"] not in sent_jobs]

    if not new_jobs:
        await context.bot.send_message(
            chat_id=chat_id,
            text="😴 Pas de nouvelles missions pour le moment. Je réessaierai plus tard !",
        )
        return

    # Limit to 5 per batch to avoid spam / rate limits
    batch = new_jobs[:5]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ {len(new_jobs)} nouvelles missions trouvées ! Génération des candidatures pour les {len(batch)} premières...",
    )

    for job in batch:
        try:
            candidature = await generate_candidature(job, profil)
            message = format_job_message(job, candidature)
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
            sent_jobs.add(job["id"])
        except Exception as e:
            logger.error(f"Error processing job {job['id']}: {e}")
            # Fallback: send without markdown
            fallback = (
                f"🎯 {job['title']}\n"
                f"🏢 {job['company']}  •  📡 {job['source']}\n"
                f"🔗 {job['url']}\n\n"
                f"✉️ Candidature :\n{candidature}"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=fallback)
                sent_jobs.add(job["id"])
            except Exception as e2:
                logger.error(f"Fallback also failed for {job['id']}: {e2}")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Terminé ! {len(batch)} candidatures générées. Copie-colle et envoie !",
    )


async def auto_command(update: Update, context) -> None:
    """Toggle auto-prospection every 6 hours."""
    msg = update.message or update.callback_query.message
    chat_id = msg.chat_id

    auto_enabled = context.bot_data.get("auto_enabled", False)
    auto_enabled = not auto_enabled
    context.bot_data["auto_enabled"] = auto_enabled

    if auto_enabled:
        context.bot_data["auto_chat_id"] = chat_id
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚡ Auto-prospection ACTIVÉE ! Je chercherai des missions toutes les 6h.",
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="💤 Auto-prospection DÉSACTIVÉE.",
        )


async def profil_command(update: Update, context) -> None:
    """Display current profile."""
    msg = update.message or update.callback_query.message
    chat_id = msg.chat_id

    profil = context.bot_data.get("profile", PROFIL_DEFAUT.copy())
    # Ensure profile is saved
    if "profile" not in context.bot_data:
        context.bot_data["profile"] = profil

    text = (
        f"👤 *Ton profil :*\n\n"
        f"📛 *Nom :* {escape_md(profil['nom'])}\n"
        f"💻 *Compétences :* {escape_md(profil['competences'])}\n"
        f"📝 *Expérience :* {escape_md(profil['experience'])}\n"
        f"🔗 *Portfolio :* {escape_md(profil['portfolio'])}\n\n"
        f"Pour modifier, envoie :\n"
        f"`/setprofil nom Ton Nom`\n"
        f"`/setprofil competences React, Vue, Node`\n"
        f"`/setprofil experience Ton experience`\n"
        f"`/setprofil portfolio https://ton\\-site\\.com`"
    )

    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="MarkdownV2",
    )


async def setprofil_command(update: Update, context) -> None:
    """Modify a profile field. Usage: /setprofil <field> <value>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage : /setprofil <champ> <valeur>\n"
            "Champs : nom, competences, experience, portfolio"
        )
        return

    field = context.args[0].lower()
    value = " ".join(context.args[1:])

    valid_fields = {"nom", "competences", "experience", "portfolio"}
    if field not in valid_fields:
        await update.message.reply_text(f"❌ Champ invalide. Champs valides : {', '.join(valid_fields)}")
        return

    profil = context.bot_data.get("profile", PROFIL_DEFAUT.copy())
    profil[field] = value
    context.bot_data["profile"] = profil

    await update.message.reply_text(f"✅ Profil mis à jour !\n{field} → {value}")


async def aide(update: Update, context) -> None:
    """Show help message."""
    msg = update.message or update.callback_query.message
    chat_id = msg.chat_id

    text = (
        "📖 *Commandes disponibles :*\n\n"
        "🔍 /missions \\- Chercher des missions maintenant\n"
        "⚡ /auto \\- Activer/désactiver la prospection auto \\(6h\\)\n"
        "👤 /profil \\- Voir ton profil\n"
        "✏️ /setprofil \\- Modifier ton profil\n"
        "❓ /aide \\- Cette aide\n"
        "🏠 /start \\- Menu principal"
    )

    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="MarkdownV2",
    )


async def handle_callback(update: Update, context) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    action = query.data
    if action == "missions":
        await missions_command(update, context)
    elif action == "auto":
        await auto_command(update, context)
    elif action == "profil":
        await profil_command(update, context)
    elif action == "aide":
        await aide(update, context)


# ---------------------------------------------------------------------------
# Scheduled prospection
# ---------------------------------------------------------------------------
async def scheduled_prospection(context) -> None:
    """Called every 6h by JobQueue."""
    if not context.bot_data.get("auto_enabled", False):
        return

    chat_id = context.bot_data.get("auto_chat_id")
    if not chat_id:
        logger.warning("Auto-prospection enabled but no chat_id set.")
        return

    logger.info("Running scheduled prospection...")

    profil = context.bot_data.get("profile", PROFIL_DEFAUT.copy())
    sent_jobs: set = context.bot_data.setdefault("sent_jobs", set())

    jobs = await fetch_jobs()
    new_jobs = [j for j in jobs if j["id"] not in sent_jobs]

    if not new_jobs:
        return

    batch = new_jobs[:5]
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔄 *Prospection auto :* {len(new_jobs)} nouvelles missions trouvées\\!",
        parse_mode="MarkdownV2",
    )

    for job in batch:
        try:
            candidature = await generate_candidature(job, profil)
            message = format_job_message(job, candidature)
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
            sent_jobs.add(job["id"])
        except Exception as e:
            logger.error(f"Scheduled: error processing job {job['id']}: {e}")
            fallback = (
                f"🎯 {job['title']}\n"
                f"🏢 {job['company']}  •  📡 {job['source']}\n"
                f"🔗 {job['url']}\n\n"
                f"✉️ Candidature :\n{candidature}"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=fallback)
                sent_jobs.add(job["id"])
            except Exception as e2:
                logger.error(f"Scheduled fallback failed: {e2}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Start the bot."""
    # Health check server for Koyeb
    Thread(target=run_health_server, daemon=True).start()

    # Persistence
    persistence = PicklePersistence(filepath="bot_data.pickle")

    # Build application
    app = Application.builder().token(TELEGRAM_TOKEN).persistence(persistence).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("missions", missions_command))
    app.add_handler(CommandHandler("auto", auto_command))
    app.add_handler(CommandHandler("profil", profil_command))
    app.add_handler(CommandHandler("setprofil", setprofil_command))
    app.add_handler(CommandHandler("aide", aide))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Scheduled job: every 6 hours
    app.job_queue.run_repeating(scheduled_prospection, interval=6 * 3600, first=60)

    logger.info("Bot started! Polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
