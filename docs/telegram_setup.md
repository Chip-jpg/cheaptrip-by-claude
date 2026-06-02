# Telegram Bot Setup

## Step 1 — Create a bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g., "My Travel Deals Bot")
4. Choose a username ending in `bot` (e.g., `mytravel_deals_bot`)
5. BotFather replies with your **bot token** — looks like `123456789:AAFxxx...`

Save this as `TELEGRAM_BOT_TOKEN` in your `.env` file.

## Step 2 — Get your chat ID

### Personal chat (alerts only visible to you)

1. Start a conversation with your new bot — open it and send `/start`
2. Visit this URL in your browser (replace `YOUR_TOKEN`):
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat":{"id":XXXXXXX}` in the JSON — that's your chat ID

Save this as `TELEGRAM_CHAT_ID` in your `.env`.

### Group chat (share deals with others)

1. Add your bot to the group
2. Send a message in the group
3. Use `getUpdates` as above — the group chat ID is a negative number like `-1001234567890`

## Step 3 — Test the connection

```bash
python main.py cycle
```

If Telegram is configured correctly, you'll see a startup message in your chat within a few seconds.

## Tip: Mute the bot

Once running, you may want to mute notifications from the bot and only check when you want deals. In Telegram, long-press the conversation → Mute.

## Rate limits

The engine enforces its own limits:
- Maximum 5 instant alerts per hour
- 1 digest per day

These prevent Telegram from rate-limiting your bot.
