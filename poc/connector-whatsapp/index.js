/**
 * GroupGuard POC — WA Connector (Reader)
 *
 * מספר קורא בלבד, מבוסס Baileys (ספק לא-רשמי, פרוטוקול Multi-Device —
 * מסמך האפיון סעיף 10.2, אופציה B). מטרתו היחידה: להיות חבר בקבוצת הוואטסאפ
 * המנוטרת, לקרוא את כל התעבורה, ולהעביר כל אירוע כ-HTTP webhook מנורמל
 * לשירות הליבה (Python/FastAPI, /webhooks/whatsapp/reader).
 *
 * לפי ההחלטה הארכיטקטונית (סעיף 10.2): המספר הזה *לא* שולח הודעות לקבוצה
 * ולא הודעות פרטיות יזומות — למעט הודעת הגילוי החד-פעמית (F-2.3). כל שליחת
 * ההתראות מתבצעת ע"י מספר נפרד, עסקי ורשמי, דרך WhatsApp Business Cloud API
 * (ראו app/providers/sender_cloud_api.py בצד הפייתוני).
 *
 * הרצה: npm install && npm start   (ראו scripts/run_connector.sh)
 * בהרצה הראשונה יוצג QR בטרמינל לסריקה מהטלפון של המספר הווירטואלי.
 */
require("dotenv").config({ path: "../.env" });
require("dotenv").config(); // fallback אם ה-.env יושב גם באותה תיקייה

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
} = require("@whiskeysockets/baileys");
const { Boom } = require("@hapi/boom");
const express = require("express");
const pino = require("pino");
const qrcodeTerminal = require("qrcode-terminal");
const fs = require("fs");
const path = require("path");

const logger = pino({ level: process.env.LOG_LEVEL === "DEBUG" ? "debug" : "warn" });

const CORE_SERVICE_URL = process.env.CORE_SERVICE_URL || "http://localhost:8000";
const CONNECTOR_SHARED_SECRET = process.env.CONNECTOR_SHARED_SECRET || "";
const AUTH_DIR = process.env.CONNECTOR_AUTH_DIR || path.join(__dirname, "auth");
const MONITORED_GROUP_ID = process.env.MONITORED_GROUP_ID || "";
// מספר הטלפון של הסוכן (E.164). אם מוגדר — מתחברים באמצעות קוד צימוד
// (Pairing Code) במקום סריקת QR. משאירים ריק כדי לחזור לזרימת ה-QR הרגילה.
const READER_WHATSAPP_NUMBER = (process.env.READER_WHATSAPP_NUMBER || "").replace(/\D/g, "");
const SEND_DISCLOSURE = (process.env.SEND_DISCLOSURE_MESSAGE || "true").toLowerCase() === "true";
const DISCLOSURE_TEXT =
  process.env.DISCLOSURE_MESSAGE_TEXT ||
  'קבוצה זו מנוטרת ע"י GroupGuard לצורך אכיפת חוקי הקבוצה.';
const CONTROL_PORT = parseInt(process.env.CONNECTOR_CONTROL_PORT || "3001", 10);
// רשת הגנה נוספת (defense-in-depth) נגד ניתוח הודעות היסטוריות (F: פרטיות —
// "לנטר רק מרגע ההצטרפות"): מעבר לשכבות ההגנה הקיימות — (1) לא מאזינים כלל
// לאירוע messaging-history.set של Baileys (סנכרון היסטוריה מרובה-מכשירים),
// ו-(2) מסננים ב-messages.upsert רק type === "notify" (הודעות חיות בזמן אמת,
// לא "append" שהוא backfill היסטורי) — נוסף כאן בדיקת "גיל" ההודעה: אם
// חותמת הזמן שלה ישנה יותר מהסף הזה, מתעלמים ולא מעבירים לליבה. זו רשת
// הגנה תיאורטית בלבד (WhatsApp לא אמור לשלוח היסטוריה כ-notify כלל), אך
// מבטיחה שגם תרחיש קצה בלתי צפוי לא יגרום לניתוח הודעות שקדמו לצירוף.
const MAX_MESSAGE_AGE_SECONDS = parseInt(process.env.MAX_MESSAGE_AGE_SECONDS || "300", 10);

let sock = null;
let isConnected = false;
let lastStatusDetail = "מאתחל...";

// --------------------------------------------------------------------------
// שליחת אירוע מנורמל לשירות הליבה
// --------------------------------------------------------------------------
async function postEvent(event) {
  try {
    const res = await fetch(`${CORE_SERVICE_URL}/webhooks/whatsapp/reader`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Connector-Secret": CONNECTOR_SHARED_SECRET,
      },
      body: JSON.stringify(event),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      logger.warn({ status: res.status, body }, "core service דחה את האירוע");
    }
  } catch (err) {
    logger.error({ err: err.message }, "נכשלה שליחת אירוע לשירות הליבה — ודא/י שהוא רץ");
  }
}

// --------------------------------------------------------------------------
// עזרי מיפוי הודעות Baileys -> סכמת האירועים הפנימית
// --------------------------------------------------------------------------
function isMonitoredGroup(jid) {
  if (!jid || !jid.endsWith("@g.us")) return false;
  if (!MONITORED_GROUP_ID) return true; // ריק = כל הקבוצות (לא מומלץ, אך נתמך)
  return jid === MONITORED_GROUP_ID;
}

function extractTextAndType(message) {
  if (!message) return { type: "other", text: null };
  if (message.conversation) return { type: "text", text: message.conversation };
  if (message.extendedTextMessage) return { type: "text", text: message.extendedTextMessage.text };
  if (message.imageMessage) return { type: "image", text: message.imageMessage.caption || null };
  if (message.stickerMessage) return { type: "sticker", text: null };
  if (message.videoMessage) return { type: "video", text: message.videoMessage.caption || null }; // מטא-דאטה בלבד ב-POC/MVP
  if (message.audioMessage) return { type: "audio", text: null }; // מטא-דאטה בלבד ב-POC/MVP (F-3.1)
  if (message.documentMessage) return { type: "document", text: message.documentMessage.fileName || null };
  if (message.locationMessage) return { type: "location", text: null };
  if (message.contactMessage) return { type: "contact", text: message.contactMessage.displayName || null };
  if (message.pollCreationMessage) return { type: "poll", text: message.pollCreationMessage.name || null };
  return { type: "other", text: null };
}

function getMediaMessagePart(message) {
  if (!message) return null;
  if (message.imageMessage) return { part: message.imageMessage, mimetype: message.imageMessage.mimetype };
  if (message.stickerMessage) return { part: message.stickerMessage, mimetype: message.stickerMessage.mimetype || "image/webp" };
  return null;
}

async function buildBaseEvent(msg, eventType) {
  const jid = msg.key.remoteJid;
  const senderJid = msg.key.participant || msg.key.remoteJid;
  const senderPhone = senderJid ? "+" + senderJid.split("@")[0] : null;
  const senderName = msg.pushName || null;
  const ctx = (msg.message && (msg.message.extendedTextMessage?.contextInfo || msg.message?.imageMessage?.contextInfo)) || {};

  return {
    event_type: eventType,
    provider_message_id: msg.key.id,
    group_id: jid,
    sender_phone: senderPhone,
    sender_name: senderName,
    is_forwarded: !!ctx.isForwarded,
    reply_to_provider_id: ctx.stanzaId || null,
    sent_at: msg.messageTimestamp
      ? new Date(Number(msg.messageTimestamp) * 1000).toISOString()
      : new Date().toISOString(),
  };
}

// --------------------------------------------------------------------------
// טיפול בהודעות נכנסות (messages.upsert)
// --------------------------------------------------------------------------
async function handleIncomingMessage(msg) {
  const jid = msg.key.remoteJid;
  if (!isMonitoredGroup(jid)) return;
  if (msg.key.fromMe) return; // לא מעניין אותנו את מה שהמספר הקורא "שלח" (הודעת הגילוי בלבד, ומטופלת בנפרד)

  // רשת הגנה נוספת: התעלמות מהודעה שחותמת הזמן שלה ישנה מדי — ראו הסבר מלא
  // ליד MAX_MESSAGE_AGE_SECONDS למעלה. לא אמור לקרות בפועל (WhatsApp לא שולח
  // היסטוריה כ-notify), אך זו הגנה מפורשת שמונעת בכל מקרה ניתוח של הודעות
  // שקדמו לצירוף מספר ה-Reader לקבוצה.
  if (msg.messageTimestamp) {
    const ageSeconds = Date.now() / 1000 - Number(msg.messageTimestamp);
    if (ageSeconds > MAX_MESSAGE_AGE_SECONDS) {
      logger.warn(
        { jid, ageSeconds: Math.round(ageSeconds) },
        "התעלמות מהודעה ישנה מדי (כנראה היסטורית) — לא מועברת לליבה"
      );
      return;
    }
  }

  // עריכה/מחיקה מגיעות כ-protocolMessage
  const proto = msg.message && msg.message.protocolMessage;
  if (proto) {
    const targetId = proto.key ? proto.key.id : null;
    if (!targetId) return;
    if (proto.type === 14 /* MESSAGE_EDIT */ || proto.type === "MESSAGE_EDIT") {
      const { text } = extractTextAndType(proto.editedMessage || {});
      await postEvent({
        event_type: "message_edit",
        provider_message_id: targetId,
        group_id: jid,
        text,
      });
    } else if (proto.type === 0 /* REVOKE */ || proto.type === "REVOKE") {
      await postEvent({ event_type: "message_delete", provider_message_id: targetId, group_id: jid });
    }
    return;
  }

  const { type: msgType, text } = extractTextAndType(msg.message);
  const base = await buildBaseEvent(msg, "message");
  const event = { ...base, msg_type: msgType, text };

  const mediaPart = getMediaMessagePart(msg.message);
  if (mediaPart) {
    try {
      const buffer = await downloadMediaMessage(msg, "buffer", {});
      event.media_base64 = buffer.toString("base64");
      event.media_mimetype = mediaPart.mimetype;
    } catch (err) {
      logger.error({ err: err.message }, "נכשלה הורדת מדיה");
    }
  }

  await postEvent(event);
}

// --------------------------------------------------------------------------
// אירועי קבוצה: הצטרפות/עזיבה/הסרה/שינוי מנהלים (F-3.2)
// --------------------------------------------------------------------------
function bindGroupEvents(sockInstance) {
  sockInstance.ev.on("group-participants.update", async (update) => {
    if (!isMonitoredGroup(update.id)) return;
    const actionMap = { add: "group_join", remove: "group_leave", promote: "group_admin_change", demote: "group_admin_change" };
    const eventType = actionMap[update.action] || "group_subject_change";
    for (const participant of update.participants || []) {
      await postEvent({
        event_type: eventType,
        group_id: update.id,
        sender_phone: "+" + participant.split("@")[0],
        detail: update.action,
      });
    }
  });
}

// --------------------------------------------------------------------------
// הודעת גילוי חד-פעמית (F-2.3) — הפעולה היחידה שהמספר הקורא יוזם כלפי הקבוצה
// --------------------------------------------------------------------------
async function maybeSendDiscoveryMessage() {
  if (!SEND_DISCLOSURE || !MONITORED_GROUP_ID) return;
  const markerPath = path.join(AUTH_DIR, ".discovery_sent");
  if (fs.existsSync(markerPath)) return;
  try {
    await sock.sendMessage(MONITORED_GROUP_ID, { text: DISCLOSURE_TEXT });
    fs.writeFileSync(markerPath, new Date().toISOString());
    logger.warn("הודעת גילוי נשלחה לקבוצה");
  } catch (err) {
    logger.error({ err: err.message }, "נכשלה שליחת הודעת גילוי");
  }
}

// --------------------------------------------------------------------------
// הפעלת חיבור Baileys, עם reconnect אוטומטי
// --------------------------------------------------------------------------
async function startConnector() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  sock = makeWASocket({
    auth: state,
    logger,
    printQRInTerminal: false, // מציגים ידנית למטה כדי לשלוט בפורמט
  });

  sock.ev.on("creds.update", saveCreds);
  bindGroupEvents(sock);

  // --- קוד צימוד (Pairing Code) במקום QR, כשמוגדר READER_WHATSAPP_NUMBER ---
  // רץ פעם אחת בלבד: אחרי שכבר מחוברים (auth/ קיים, state.creds.registered=true)
  // אין צורך לצמד שוב, וה-QR/קוד לא יופיעו כלל.
  if (READER_WHATSAPP_NUMBER && !state.creds.registered) {
    setTimeout(async () => {
      try {
        const code = await sock.requestPairingCode(READER_WHATSAPP_NUMBER);
        console.log("\n>>> קוד הצימוד שלכם (הזינו אותו בוואטסאפ במספר " + READER_WHATSAPP_NUMBER +
          " -> מכשירים מקושרים -> קישור מכשיר עם מספר טלפון במקום):\n");
        console.log("    " + code + "\n");
      } catch (err) {
        logger.error({ err: err.message }, "נכשלה בקשת קוד צימוד — בדקו את READER_WHATSAPP_NUMBER או נסו סריקת QR");
      }
    }, 3000); // השהיה קצרה כדי לתת לחיבור להתייצב לפני בקשת הקוד (מומלץ ע"י Baileys)
  }

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr && !READER_WHATSAPP_NUMBER) {
      console.log("\n>>> סרוק/י את קוד ה-QR הבא עם וואטסאפ במספר הווירטואלי (מכשיר מקושר):\n");
      qrcodeTerminal.generate(qr, { small: true });
    }
    if (connection === "open") {
      isConnected = true;
      lastStatusDetail = "מחובר";
      logger.warn("Baileys מחובר בהצלחה");
      maybeSendDiscoveryMessage();
    } else if (connection === "close") {
      isConnected = false;
      const statusCode = new Boom(lastDisconnect?.error)?.output?.statusCode;
      lastStatusDetail = `מנותק (קוד ${statusCode})`;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      logger.warn({ statusCode, shouldReconnect }, "החיבור נסגר");
      if (shouldReconnect) {
        setTimeout(startConnector, 3000);
      } else {
        logger.error("המספר נותק/הוסר (loggedOut) — יש לסרוק QR מחדש (מחקו את תיקיית auth/ כדי לאתחל).");
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      try {
        await handleIncomingMessage(msg);
      } catch (err) {
        logger.error({ err: err.message }, "שגיאה בטיפול בהודעה נכנסת");
      }
    }
  });
}

// --------------------------------------------------------------------------
// שרת בקרה מקומי קטן — סטטוס + שליחת הודעת גילוי ידנית (נצרך ע"י הצד הפייתוני
// דרך app/providers/reader_baileys.py)
// --------------------------------------------------------------------------
const controlApp = express();
controlApp.use(express.json());

controlApp.get("/status", (req, res) => {
  res.json({ connected: isConnected, detail: lastStatusDetail });
});

controlApp.post("/send-discovery", async (req, res) => {
  const { groupId, text } = req.body || {};
  if (!sock || !isConnected) return res.status(503).json({ error: "not_connected" });
  try {
    const result = await sock.sendMessage(groupId || MONITORED_GROUP_ID, { text: text || DISCLOSURE_TEXT });
    res.json({ id: result?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

controlApp.listen(CONTROL_PORT, () => {
  logger.warn(`Control API של ה-connector מאזין על פורט ${CONTROL_PORT}`);
});

startConnector().catch((err) => {
  logger.error({ err: err.message }, "כשל קריטי באתחול ה-connector");
  process.exit(1);
});
