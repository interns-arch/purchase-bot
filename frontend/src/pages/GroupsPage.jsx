import { useEffect, useRef, useState } from "react";
import { Layout } from "../components/Layout";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { downloadGroupFile, getGroup, listActivity, listGroups } from "../api/groups";

const LIVE_EVERY_MS = 8000;
const ACTIVITY_EVERY_MS = 4000;
const ACTIVITY_KEEP = 150;

const KIND_SAYS = {
  received: "took a message from",
  "received file": "took a file from",
  posted: "replied in",
  "posted file": "sent a file in",
  acknowledged: "said 'received' in",
  "stock request": "",
  "group created": "created",
};

// What the bot did, as one line: "10:59 · Team · Seema ji → took a file: stock 30.xlsx".
function ActivityRow({ item, onOpen }) {
  const says = KIND_SAYS[item.kind] ?? item.kind;
  return (
    <button type="button" className="activity__row" onClick={() => item.group_id && onOpen(item.group_id)}>
      <span className="activity__time">{formatShort(item.created_at)}</span>
      <span className={"activity__kind activity__kind--" + item.kind.replace(/\s+/g, "-")}>{item.kind}</span>
      <span className="activity__what">
        {says ? <>{item.who ? `${item.who} — ` : ""}bot {says} <b>{item.group}</b>: </> : null}
        {item.text}
      </span>
    </button>
  );
}

const shortTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "2-digit",
  month: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function formatShort(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const parts = Object.fromEntries(shortTime.formatToParts(date).map((p) => [p.type, p.value]));
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function GroupListItem({ group, active, onOpen }) {
  return (
    <button type="button" className={"chat-item" + (active ? " chat-item--active" : "")} onClick={onOpen}>
      <div className="chat-item__top">
        <span className="chat-item__name">{group.name}</span>
        <span className="chat-item__time">{formatShort(group.last_at)}</span>
      </div>
      <div className="chat-item__meta">
        {group.role}
        {group.member === false && <span className="takeover-badge">number not in this group</span>}
      </div>
      <div className="chat-item__preview">{group.preview}</div>
    </button>
  );
}

// The bot's own posts (and Alam sir's, from the same phone) sit on the right,
// like in WhatsApp; everyone else on the left with their name.
function GroupMessage({ message }) {
  const toast = useToast();
  const ours = message.by === "bot" || message.by === "owner";
  const isFile = message.kind === "document" || message.kind === "image";
  return (
    <div className={"chat-msg " + (ours ? "chat-msg--out" : "chat-msg--in") + (message.by === "owner" ? " chat-msg--admin" : "")}>
      <div className="chat-msg__bubble">
        {!ours && <div className="chat-msg__label">{message.name}{message.by === "team" ? " · team" : ""}</div>}
        {isFile && (
          <div className="chat-msg__file">
            <span>{message.kind === "image" ? "🖼️" : "📎"} {message.filename || "file"}</span>
            {message.media_id && (
              <button
                type="button"
                className="btn btn--ghost chat-msg__download"
                onClick={() =>
                  downloadGroupFile(message.media_id, message.filename).catch((error) =>
                    toast.error(extractErrorMessage(error, "Could not download the file."))
                  )
                }
              >
                Download
              </button>
            )}
          </div>
        )}
        {message.text && <div className="chat-msg__text">{message.text}</div>}
        <div className="chat-msg__footer">
          {ours ? message.name : message.number} · {formatShort(message.created_at)}
        </div>
      </div>
    </div>
  );
}

export function GroupsPage() {
  const toast = useToast();
  const [groups, setGroups] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [openId, setOpenId] = useState(null);
  const [thread, setThread] = useState(null);
  const [live, setLive] = useState(true);
  const [activity, setActivity] = useState([]);
  const threadEndRef = useRef(null);
  const lastMessageId = useRef(null);

  // The live feed: only rows newer than what the page already has.
  async function loadActivity() {
    try {
      const newest = activity.length ? activity[0].id : 0;
      const rows = await listActivity(newest);
      if (rows.length) setActivity((old) => [...rows, ...old].slice(0, ACTIVITY_KEEP));
    } catch (error) {
      // quiet: the feed refreshes again in a few seconds
    }
  }

  async function loadGroups() {
    try {
      setGroups(await listGroups());
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load the groups."));
    } finally {
      setIsLoading(false);
    }
  }

  async function loadThread(groupId) {
    if (!groupId) return;
    try {
      setThread(await getGroup(groupId));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load this group."));
    }
  }

  useEffect(() => {
    loadGroups();
    loadActivity();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(loadActivity, ACTIVITY_EVERY_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, activity]);

  useEffect(() => {
    loadThread(openId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openId]);

  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(() => {
      loadGroups();
      if (openId) loadThread(openId);
    }, LIVE_EVERY_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, openId]);

  useEffect(() => {
    const messages = thread?.messages || [];
    const newest = messages.length ? messages[messages.length - 1].id : null;
    if (newest !== lastMessageId.current) {
      lastMessageId.current = newest;
      threadEndRef.current?.scrollIntoView({ block: "end" });
    }
  }, [thread]);

  return (
    <Layout title="Groups" subtitle="The WhatsApp groups the bot is in — every message, live.">
      <section className="panel chats">
        <div className="chats__toolbar">
          <span className="chats__chip">{groups.length} groups</span>
          <label className="chats__live">
            <input type="checkbox" checked={live} onChange={(event) => setLive(event.target.checked)} /> live
          </label>
        </div>

        <div className="activity">
          <div className="activity__head">
            <b>Live: what the bot is doing</b>
            <span className="activity__hint">newest first · click a line to open that group</span>
          </div>
          {activity.length === 0 ? (
            <p className="chats__empty">Nothing yet — the first file or question in a group shows up here.</p>
          ) : (
            activity.map((item) => <ActivityRow key={item.id} item={item} onOpen={setOpenId} />)
          )}
        </div>

        <div className="chats__body">
          <div className="chats__list">
            {isLoading ? (
              <div className="page-loading">Loading…</div>
            ) : groups.length === 0 ? (
              <p className="chats__empty">The bot is not in any group yet.</p>
            ) : (
              groups.map((group) => (
                <GroupListItem key={group.id} group={group} active={group.id === openId} onOpen={() => setOpenId(group.id)} />
              ))
            )}
          </div>

          <div className="chats__pane">
            {!thread ? (
              <p className="chats__empty">Pick a group on the left.</p>
            ) : (
              <>
                <div className="chats__pane-head">
                  <span className="chats__chip">
                    {thread.name} · {thread.role} · {thread.messages.length} messages
                  </span>
                </div>
                <div className="chats__thread">
                  {thread.messages.length === 0 && <p className="chats__empty">No messages yet.</p>}
                  {thread.messages.map((message) => (
                    <GroupMessage key={message.id} message={message} />
                  ))}
                  <div ref={threadEndRef} />
                </div>
              </>
            )}
          </div>
        </div>
      </section>
    </Layout>
  );
}
