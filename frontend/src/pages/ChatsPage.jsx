import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Layout } from "../components/Layout";
import { useToast } from "../context/ToastContext";
import { extractErrorMessage } from "../api/client";
import { downloadChatFile, getChat, heldMessage, listChats, sendByHand, switchTakeover } from "../api/chats";
import { downloadGroupFile, getGroup, listActivity, listGroups } from "../api/groups";

// One inbox for the bot's WhatsApp (20 Nov 2026): one-to-one chats and the
// WhatsApp groups in a single list, filtered by who it is -- the old Groups
// page lives here as the "Groups" filter (/groups redirects to it).

const LIVE_EVERY_MS = 6000;
const ACTIVITY_KEEP = 150;

const TZ = "Asia/Kolkata";
const clock = new Intl.DateTimeFormat("en-IN", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true });
const dayMonth = new Intl.DateTimeFormat("en-IN", { timeZone: TZ, day: "numeric", month: "short" });
const dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

function asDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

// "7:15 pm" today, "19 Nov" before.
function listTime(value) {
  const date = asDate(value);
  if (!date) return "";
  return dayKey.format(date) === dayKey.format(new Date()) ? clock.format(date).toLowerCase() : dayMonth.format(date);
}

function messageTime(value) {
  const date = asDate(value);
  return date ? clock.format(date).toLowerCase() : "";
}

function dayLabel(value) {
  const date = asDate(value);
  if (!date) return "";
  const key = dayKey.format(date);
  if (key === dayKey.format(new Date())) return "Today";
  if (key === dayKey.format(new Date(Date.now() - 864e5))) return "Yesterday";
  return dayMonth.format(date);
}

const TEAM_TAGS = new Set(["admin", "approver", "purchase team", "accounts"]);

// Every chat and group as one row shape.
function chatRow(chat) {
  const tags = chat.tags || [];
  let kind = "other";
  if (tags.includes("vendor")) kind = "vendor";
  else if (tags.some((t) => TEAM_TAGS.has(t))) kind = "team";
  else if (tags.includes("customer")) kind = "customer";
  return {
    key: `c:${chat.number}`,
    type: "chat",
    id: chat.number,
    kind,
    name: chat.name || chat.number,
    sub: tags.join(", ") || chat.number,
    last_at: chat.last_at,
    preview: chat.preview,
    count: chat.message_count,
    flag: chat.takeover?.on ? "you have it" : chat.takeover?.pending > 0 ? `${chat.takeover.pending} unanswered` : null,
  };
}

function groupRow(group) {
  return {
    key: `g:${group.id}`,
    type: "group",
    id: group.id,
    kind: "group",
    groupKind: group.role === "team" ? "team" : group.role?.startsWith("vendor") ? "vendor" : "other",
    name: group.name,
    sub: group.role,
    last_at: group.last_at,
    preview: group.preview,
    count: null,
    flag: group.active === false ? "view only" : group.member === false ? "not a member" : null,
  };
}

const FILTERS = [
  { id: "all", label: "All", test: () => true },
  { id: "vendors", label: "Vendors", test: (r) => r.kind === "vendor" || r.groupKind === "vendor" },
  { id: "team", label: "Team", test: (r) => r.kind === "team" || r.groupKind === "team" },
  { id: "customers", label: "Customers", test: (r) => r.kind === "customer" },
  { id: "groups", label: "Groups", test: (r) => r.type === "group" },
];

function initials(name) {
  const words = String(name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/);
  return ((words[0]?.[0] || "?") + (words[1]?.[0] || "")).toUpperCase();
}

function GroupIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <circle cx="9" cy="8" r="3.2" />
      <path d="M3.5 19c.6-3 2.8-4.6 5.5-4.6s4.9 1.6 5.5 4.6" />
      <circle cx="17" cy="9" r="2.4" />
      <path d="M16 14.5c2.2.1 3.9 1.5 4.5 4" />
    </svg>
  );
}

function Avatar({ row }) {
  return <span className={"inbox-avatar" + (row.type === "group" ? " inbox-avatar--group" : "")}>{row.type === "group" ? <GroupIcon /> : initials(row.name)}</span>;
}

function Row({ row, active, onOpen }) {
  return (
    <button type="button" className={"inbox-row" + (active ? " inbox-row--active" : "")} onClick={onOpen}>
      <Avatar row={row} />
      <span className="inbox-row__body">
        <span className="inbox-row__line">
          <span className="inbox-row__name">{row.name}</span>
          <span className="inbox-row__time">{listTime(row.last_at)}</span>
        </span>
        <span className="inbox-row__preview">{row.preview || "—"}</span>
        <span className="inbox-row__line inbox-row__line--meta">
          <span className="inbox-row__tag">{row.sub}</span>
          {row.flag ? <span className="inbox-row__flag">{row.flag}</span> : row.count != null ? <span>{row.count} msgs</span> : null}
        </span>
      </span>
    </button>
  );
}

const STATUS_MARK = { sent: "✓", delivered: "✓✓", read: "✓✓", failed: "!" };

function Bubble({ outgoing, label, text, file, footer, status, error, admin }) {
  return (
    <div className={"inbox-msg " + (outgoing ? "inbox-msg--out" : "inbox-msg--in") + (admin ? " inbox-msg--admin" : "")}>
      <div className="inbox-msg__bubble">
        {label ? <div className="inbox-msg__label">{label}</div> : null}
        {file}
        {text ? <div className="inbox-msg__text">{text}</div> : null}
        <div className="inbox-msg__footer">
          {footer}
          {status ? (
            <span className={"inbox-msg__status inbox-msg__status--" + status} title={error || status}>
              {STATUS_MARK[status] || status}
            </span>
          ) : null}
        </div>
        {error ? <div className="inbox-msg__error">Not delivered: {error}</div> : null}
      </div>
    </div>
  );
}

function FileLine({ kind, filename, onDownload }) {
  return (
    <div className="inbox-msg__file">
      <span>{kind === "image" ? "Photo" : "File"}: {filename || "file"}</span>
      {onDownload ? (
        <button type="button" className="inbox-link" onClick={onDownload}>
          Download
        </button>
      ) : null}
    </div>
  );
}

// Messages with a date line between days, like WhatsApp.
function Thread({ messages, render, endRef }) {
  let lastDay = null;
  return (
    <div className="inbox-thread">
      {messages.length === 0 ? <p className="inbox-empty">No messages yet.</p> : null}
      {messages.map((message) => {
        const day = dayLabel(message.created_at);
        const divider = day !== lastDay ? <div className="inbox-day" key={`d-${message.id}`}>{day}</div> : null;
        lastDay = day;
        return [divider, render(message)];
      })}
      <div ref={endRef} />
    </div>
  );
}

function ChatMessage({ message }) {
  const toast = useToast();
  const outgoing = message.direction === "out";
  const isFile = message.kind === "document" || message.kind === "image";
  return (
    <Bubble
      key={message.id}
      outgoing={outgoing}
      admin={Boolean(message.sent_by)}
      label={message.kind === "template" ? "Template" : null}
      text={message.text}
      file={
        isFile ? (
          <FileLine
            kind={message.kind}
            filename={message.filename}
            onDownload={
              message.has_file
                ? () => downloadChatFile(message.id, message.filename).catch((e) => toast.error(extractErrorMessage(e, "Could not download the file.")))
                : null
            }
          />
        ) : null
      }
      footer={`${outgoing ? message.sent_by || "Bot" : "They"} ${messageTime(message.created_at)}`}
      status={outgoing ? message.status : null}
      error={message.error}
    />
  );
}

function GroupMessage({ message }) {
  const toast = useToast();
  const ours = message.by === "bot" || message.by === "owner";
  const isFile = message.kind === "document" || message.kind === "image";
  return (
    <Bubble
      key={message.id}
      outgoing={ours}
      admin={message.by === "owner"}
      label={ours ? null : `${message.name}${message.by === "team" ? " · team" : ""}`}
      text={message.text}
      file={
        isFile ? (
          <FileLine
            kind={message.kind}
            filename={message.filename}
            onDownload={
              message.media_id
                ? () => downloadGroupFile(message.media_id, message.filename).catch((e) => toast.error(extractErrorMessage(e, "Could not download the file.")))
                : null
            }
          />
        ) : null
      }
      footer={`${ours ? message.name : message.number} ${messageTime(message.created_at)}`}
    />
  );
}

const HANDOVER_SAYS = {
  "nothing pending": "Back with the bot — nothing was waiting for a reply.",
  acknowledgment: "Back with the bot — the last message was only a thanks, so no reply was sent.",
  answered: "Back with the bot — it answered their last message.",
  "still taken over": "Taken over again while the bot was replying — its reply was held.",
  failed: "Back with the bot — but it could not answer the last message; please check the chat.",
  "already with the bot": "Already with the bot.",
  "already running": "The bot is already answering.",
};

// HUMAN TAKEOVER (founder, 28 Oct): the bot goes quiet on this chat, you
// reply by hand, and hand it back. A state is shown only once the server confirmed it.
function TakeoverButton({ thread, onState }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const tk = thread.takeover || { on: false, version: 0 };
  async function flip() {
    if (tk.on && !window.confirm("Hand this chat back to the bot? It answers the last message if it still needs an answer.")) return;
    setBusy(true);
    try {
      const result = await switchTakeover(thread.number, !tk.on, tk.version);
      onState(result.state);
      toast.success(tk.on ? HANDOVER_SAYS[result.result] || "Back with the bot." : "You have this chat — the bot is paused.");
    } catch (error) {
      const detail = error?.response?.data?.detail;
      if (detail?.state) onState(detail.state);
      toast.error(detail?.error || extractErrorMessage(error, "Could not switch."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <button type="button" className={"inbox-btn" + (tk.on ? " inbox-btn--on" : "")} onClick={flip} disabled={busy}>
      {busy ? "…" : tk.on ? "Hand back to bot" : "Take over"}
    </button>
  );
}

function TakeoverNote({ thread }) {
  const tk = thread.takeover;
  if (!tk) return null;
  const pending = (tk.pending || []).length;
  if (tk.on) {
    return (
      <div className="inbox-note inbox-note--warn">
        Bot paused — {tk.by || "admin"} has this chat since {listTime(tk.since)}. You reply below.
        {pending ? ` ${pending} unanswered.` : ""}
      </div>
    );
  }
  if (tk.resume?.status === "interrupted") {
    return <div className="inbox-note inbox-note--warn">The hand-back was cut off by a restart — check the chat.</div>;
  }
  return pending ? <div className="inbox-note">{pending} message(s) waiting for an answer.</div> : null;
}

// What the bot wanted to say while you had the chat — kept, not lost.
function HeldBack({ thread, onState }) {
  const toast = useToast();
  const [busy, setBusy] = useState("");
  const held = thread.takeover?.held || [];
  if (!held.length) return null;
  async function act(item, action) {
    setBusy(item.id + action);
    try {
      const result = await heldMessage(thread.number, item.id, action);
      onState(result.state);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not do that."));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="inbox-held">
      <div className="inbox-held__title">Held back from the bot while you had the chat ({held.length})</div>
      {held.map((item) => (
        <div key={item.id} className="inbox-held__item">
          <span className="inbox-held__time">{listTime(item.at)}</span>
          <span className="inbox-held__text">
            {item.kind !== "text" ? `(${item.kind}) ` : ""}
            {item.text || "—"}
          </span>
          {item.sendable ? (
            <button type="button" className="inbox-link" disabled={Boolean(busy)} onClick={() => act(item, "send")}>
              Send
            </button>
          ) : null}
          <button type="button" className="inbox-link inbox-link--muted" disabled={Boolean(busy)} onClick={() => act(item, "drop")}>
            Dismiss
          </button>
        </div>
      ))}
    </div>
  );
}

function Composer({ thread, onSent }) {
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  if (!thread.takeover?.on) return null;
  async function send() {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    try {
      const result = await sendByHand(thread.number, body);
      setText("");
      onSent(result.state);
    } catch (error) {
      toast.error(extractErrorMessage(error, "Not sent."));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="inbox-composer">
      <textarea
        rows={1}
        placeholder="Reply from the bot's number — Enter to send, Shift+Enter for a new line"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            send();
          }
        }}
      />
      <button type="button" className="inbox-btn inbox-btn--on" onClick={send} disabled={busy || !text.trim()}>
        {busy ? "Sending…" : "Send"}
      </button>
    </div>
  );
}

const KIND_SAYS = {
  received: "took a message from",
  "received file": "took a file from",
  posted: "replied in",
  "posted file": "sent a file in",
  acknowledged: "said 'received' in",
  "stock request": "",
  "group created": "created",
};

// What the bot did in the groups, newest first.
function BotLog({ activity, onOpen }) {
  return (
    <div className="inbox-thread inbox-log">
      {activity.length === 0 ? <p className="inbox-empty">Nothing yet — the first file or question in a group shows up here.</p> : null}
      {activity.map((item) => {
        const says = KIND_SAYS[item.kind] ?? item.kind;
        return (
          <button type="button" key={item.id} className="inbox-log__row" onClick={() => item.group_id && onOpen(`g:${item.group_id}`)}>
            <span className="inbox-log__time">{listTime(item.created_at)}</span>
            <span className="inbox-log__kind">{item.kind}</span>
            <span className="inbox-log__what">
              {says ? (
                <>
                  {item.who ? `${item.who} — ` : ""}bot {says} <b>{item.group}</b>:{" "}
                </>
              ) : null}
              {item.text}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function ChatsPage() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const filter = FILTERS.some((f) => f.id === params.get("filter")) ? params.get("filter") : "all";
  const [query, setQuery] = useState("");
  const [live, setLive] = useState(true);
  const [chats, setChats] = useState([]);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [openKey, setOpenKey] = useState(null);
  const [thread, setThread] = useState(null);
  const [showLog, setShowLog] = useState(false);
  const [activity, setActivity] = useState([]);
  const endRef = useRef(null);
  const lastMessageId = useRef(null);

  async function loadChats(q = query) {
    try {
      setChats(await listChats(q));
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load chats."));
    }
  }

  async function loadGroups() {
    try {
      setGroups(await listGroups());
    } catch {
      // groups need Whapi; the chats still show without them
    }
  }

  async function loadActivity() {
    try {
      const newest = activity.length ? activity[0].id : 0;
      const rows = await listActivity(newest);
      if (rows.length) {
        // Merged by id: two loads that overlap (first open, live refresh) never show a line twice.
        setActivity((old) => {
          const seen = new Set(old.map((item) => item.id));
          return [...rows.filter((item) => !seen.has(item.id)), ...old].sort((x, y) => y.id - x.id).slice(0, ACTIVITY_KEEP);
        });
      }
    } catch {
      // quiet: refreshed again in a few seconds
    }
  }

  async function loadThread(key = openKey) {
    if (!key) return;
    const [type, id] = [key.slice(0, 1), key.slice(2)];
    try {
      const data = type === "c" ? await getChat(id) : await getGroup(id);
      setThread({ ...data, _key: key, _type: type === "c" ? "chat" : "group" });
    } catch (error) {
      toast.error(extractErrorMessage(error, "Could not load this conversation."));
    }
  }

  useEffect(() => {
    Promise.all([loadChats(""), loadGroups(), loadActivity()]).finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Search: chats on the server (it also searches message text), groups here.
  useEffect(() => {
    const timer = setTimeout(() => loadChats(query), 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  useEffect(() => {
    setThread(null);
    lastMessageId.current = null;
    loadThread(openKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openKey]);

  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(() => {
      loadChats();
      loadGroups();
      loadActivity();
      if (openKey) loadThread(openKey);
    }, LIVE_EVERY_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, openKey, query, activity]);

  useEffect(() => {
    const messages = thread?.messages || [];
    const newest = messages.length ? messages[messages.length - 1].id : null;
    if (newest !== lastMessageId.current) {
      lastMessageId.current = newest;
      endRef.current?.scrollIntoView({ block: "end" });
    }
  }, [thread]);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const groupRows = groups.map(groupRow).filter((r) => !needle || `${r.name} ${r.sub} ${r.preview}`.toLowerCase().includes(needle));
    return [...chats.map(chatRow), ...groupRows].sort((a, b) => String(b.last_at || "").localeCompare(String(a.last_at || "")));
  }, [chats, groups, query]);

  const counts = Object.fromEntries(FILTERS.map((f) => [f.id, rows.filter(f.test).length]));
  const shown = rows.filter(FILTERS.find((f) => f.id === filter).test);
  const openRow = rows.find((r) => r.key === openKey);

  function pickFilter(id) {
    setParams(id === "all" ? {} : { filter: id }, { replace: true });
  }

  function open(key) {
    setShowLog(false);
    setOpenKey(key);
  }

  function setTakeover(state) {
    if (state) setThread((current) => (current ? { ...current, takeover: state } : current));
  }

  return (
    <Layout title="Chats" subtitle={null} bare>
      <section className={"inbox" + (openKey || showLog ? " inbox--open" : "")}>
        <aside className="inbox-list">
          <div className="inbox-list__head">
            <h2>Chats</h2>
            <button type="button" className={"inbox-live" + (live ? " inbox-live--on" : "")} onClick={() => setLive(!live)} title="Refresh every few seconds">
              <span className="inbox-live__dot" />
              {live ? "Live" : "Paused"}
            </button>
            <button type="button" className={"inbox-link" + (showLog ? " inbox-link--on" : "")} onClick={() => setShowLog(!showLog)}>
              Bot log
            </button>
          </div>
          <input
            className="inbox-search"
            type="search"
            placeholder="Search number, name or word"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <div className="inbox-filters" role="tablist">
            {FILTERS.map((f) => (
              <button
                key={f.id}
                type="button"
                role="tab"
                aria-selected={filter === f.id}
                className={"inbox-chip" + (filter === f.id ? " inbox-chip--on" : "")}
                onClick={() => pickFilter(f.id)}
              >
                {f.label}
                <span className="inbox-chip__n">{counts[f.id]}</span>
              </button>
            ))}
          </div>
          <div className="inbox-list__rows">
            {loading ? (
              <p className="inbox-empty">Loading…</p>
            ) : shown.length === 0 ? (
              <p className="inbox-empty">{query ? "Nothing matches that search." : "Nothing here yet."}</p>
            ) : (
              shown.map((row) => <Row key={row.key} row={row} active={row.key === openKey && !showLog} onOpen={() => open(row.key)} />)
            )}
          </div>
        </aside>

        <div className="inbox-pane">
          {showLog ? (
            <>
              <header className="inbox-pane__head">
                <button type="button" className="inbox-back" onClick={() => setShowLog(false)} aria-label="Back">
                  ‹
                </button>
                <div className="inbox-pane__who">
                  <b>Bot log</b>
                  <span>What the bot did in the WhatsApp groups, newest first. Click a line to open the group.</span>
                </div>
              </header>
              <BotLog activity={activity} onOpen={open} />
            </>
          ) : !openKey ? (
            <p className="inbox-empty inbox-empty--center">Pick a chat or a group on the left.</p>
          ) : !thread ? (
            <p className="inbox-empty inbox-empty--center">Loading…</p>
          ) : (
            <>
              <header className="inbox-pane__head">
                <button type="button" className="inbox-back" onClick={() => setOpenKey(null)} aria-label="Back">
                  ‹
                </button>
                {openRow ? <Avatar row={openRow} /> : null}
                <div className="inbox-pane__who">
                  <b>{thread._type === "chat" ? thread.name || thread.number : thread.name}</b>
                  <span>
                    {thread._type === "chat"
                      ? [thread.number, ...(thread.tags || [])].join(" · ")
                      : [thread.role, `${thread.messages.length} messages`, thread.active === false ? "view only — the bot does not act here" : null]
                          .filter(Boolean)
                          .join(" · ")}
                  </span>
                </div>
                {thread._type === "chat" ? <TakeoverButton thread={thread} onState={setTakeover} /> : null}
              </header>
              {thread._type === "chat" ? (
                <>
                  <TakeoverNote thread={thread} />
                  <HeldBack thread={thread} onState={setTakeover} />
                  <Thread messages={thread.messages} endRef={endRef} render={(m) => <ChatMessage key={m.id} message={m} />} />
                  <Composer
                    thread={thread}
                    onSent={(state) => {
                      setTakeover(state);
                      loadThread(openKey);
                    }}
                  />
                </>
              ) : (
                <Thread messages={thread.messages} endRef={endRef} render={(m) => <GroupMessage key={m.id} message={m} />} />
              )}
            </>
          )}
        </div>
      </section>
    </Layout>
  );
}
