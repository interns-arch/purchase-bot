import { useEffect, useState } from "react";
import { Layout } from "../components/Layout";
import { useToast } from "../context/ToastContext";
import { apiClient, extractErrorMessage } from "../api/client";
import { formatDateTime } from "../utils/datetime";

// Vendors, purchase team and bot in one place (Founder, 15 Oct 2026): who
// sends stock consistently, who had to chase it, and what the bot did.

const PERIODS = [7, 14, 30, 90];

function Bar({ value }) {
  const tone = value >= 75 ? "var(--color-success)" : value >= 40 ? "#f59e0b" : "var(--color-danger)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div style={{ width: 90, height: 8, background: "var(--color-neutral-bg)", borderRadius: 4, overflow: "hidden" }}>
        <div style={{ width: `${Math.min(100, value)}%`, height: "100%", background: tone }} />
      </div>
      <strong style={{ fontSize: "0.85rem" }}>{value}%</strong>
    </div>
  );
}

function Stat({ label, value, hint }) {
  return (
    <div className="stat-card">
      <p className="stat-card__label">{label}</p>
      <p className="stat-card__value">{value}</p>
      {hint ? <p className="stat-card__hint">{hint}</p> : null}
    </div>
  );
}

export function TeamPerformancePage() {
  const toast = useToast();
  const [days, setDays] = useState(30);
  const [data, setData] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [view, setView] = useState("daily");
  const [openDay, setOpenDay] = useState(null);

  useEffect(() => {
    setData(null);
    apiClient
      .get("/api/team-performance", { params: { days } })
      .then((r) => setData(r.data))
      .catch((error) => toast.error(extractErrorMessage(error, "Could not load performance.")));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [days]);

  const periodPicker = (
    <div className="toolbar" style={{ gap: 6, flexWrap: "wrap" }}>
      <button type="button" className={"btn " + (view === "daily" ? "btn--primary" : "btn--ghost")} onClick={() => setView("daily")}>
        📅 Day by day
      </button>
      <button type="button" className={"btn " + (view === "summary" ? "btn--primary" : "btn--ghost")} onClick={() => setView("summary")}>
        📊 Summary
      </button>
      <span style={{ width: 16 }} />
      {PERIODS.map((p) => (
        <button key={p} type="button" className={"btn " + (p === days ? "btn--primary" : "btn--ghost")} onClick={() => setDays(p)}>
          Last {p} days
        </button>
      ))}
    </div>
  );

  if (!data) {
    return (
      <Layout title="Performance">
        {periodPicker}
        <p>Loading…</p>
      </Layout>
    );
  }

  const senders = data.vendors.filter((v) => v.days_sent > 0);
  // Only outside vendors the bot asks (registered numbers) compete; own
  // warehouses (Bijwasan, Jaipur...) are uploaded by the team.
  const best = senders.find((v) => v.registered);
  const silent = data.vendors.filter((v) => v.days_sent === 0 && v.registered);
  const shown = showAll ? data.vendors : data.vendors.filter((v) => v.days_sent > 0 || v.bot_requests > 0).slice(0, 40);
  const bot = data.bot;
  const enquiryReply = bot.enquiries_sent ? Math.round((100 * (bot.enquiries_answered || 0)) / bot.enquiries_sent) : null;

  return (
    <Layout
      title="Performance"
      subtitle={`Vendors, purchase team and bot · ${data.from} to ${data.to} · ${data.working_days} working days (Sunday off)`}
    >
      {periodPicker}

      {view === "daily" ? (
        <DailyView data={data} openDay={openDay} setOpenDay={setOpenDay} />
      ) : (
      <>
      <section className="panel">
        <div className="stat-grid">
          <Stat
            label="⭐ Most consistent vendor"
            value={best ? best.vendor : "—"}
            hint={best ? `sent stock ${best.days_sent} of ${best.working_days} days (${best.consistency}%)` : "no stock received"}
          />
          <Stat label="Vendors who sent stock" value={senders.filter((v) => v.registered).length} hint={`${silent.length} registered vendors sent nothing`} />
          <Stat label="Stock files received" value={bot.files_imported} hint={`${bot.files_failed} could not be read`} />
          <Stat
            label="Bot asked vendors"
            value={bot.stock_requests + bot.stock_reminders}
            hint={`${bot.stock_requests} requests (${data.request_time}) · ${bot.stock_reminders} reminders (${data.reminder_time})`}
          />
        </div>
      </section>

      <section className="panel">
        <h3 style={{ fontSize: "1rem", margin: "0 0 4px" }}>🏪 Vendors — who sends stock, how often, and who sent it</h3>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "0 0 10px" }}>
          Consistency = days stock arrived ÷ working days. On time = arrived before the {data.reminder_time} reminder. “Sent by”
          shows whether the vendor sent it or a team member forwarded it.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Vendor</th>
                <th>Consistency</th>
                <th>Days sent</th>
                <th>On time</th>
                <th>Usual time</th>
                <th>Sent by</th>
                <th>Bot asked</th>
                <th>Dealer Portal</th>
                <th>Last stock</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((v, i) => (
                <tr key={v.vendor_id}>
                  <td>{i + 1}</td>
                  <td>
                    <strong>{v.vendor}</strong>
                    {!v.registered ? (
                      <div style={{ fontSize: "0.75rem", color: "var(--color-text-muted)" }}>warehouse / not asked by the bot</div>
                    ) : null}
                    {v.failed_files ? <div style={{ fontSize: "0.78rem", color: "var(--color-danger)" }}>{v.failed_files} file(s) unreadable</div> : null}
                  </td>
                  <td><Bar value={v.consistency} /></td>
                  <td>{v.days_sent} / {v.working_days}</td>
                  <td>{v.days_sent ? `${v.on_time_days} / ${v.days_sent}` : "—"}</td>
                  <td>{v.usual_time || "—"}</td>
                  <td style={{ fontSize: "0.82rem" }}>
                    {v.senders.length
                      ? v.senders.map((s) => (
                          <div key={s.who}>
                            {s.who === "vendor itself" ? "🙋 vendor itself" : s.who === "website / e-mail" ? "🌐 website / e-mail" : `👤 ${s.who}`} · {s.files}
                          </div>
                        ))
                      : "—"}
                  </td>
                  <td style={{ fontSize: "0.82rem" }}>
                    {v.bot_requests + v.bot_reminders ? `${v.bot_requests} requests · ${v.bot_reminders} reminders` : "—"}
                  </td>
                  <td style={{ fontSize: "0.82rem" }}>
                    {v.portal ? `${v.portal.uploads} upload(s) · last ${v.portal.last_status} · ${v.portal.accepted} parts` : "—"}
                  </td>
                  <td style={{ fontSize: "0.82rem" }}>{v.last_received ? formatDateTime(v.last_received) : "never"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <button type="button" className="btn btn--ghost" style={{ marginTop: 8 }} onClick={() => setShowAll(!showAll)}>
          {showAll ? "Show active vendors only" : `Show all ${data.vendors.length} vendors`}
        </button>
      </section>

      <section className="panel">
        <h3 style={{ fontSize: "1rem", margin: "0 0 4px" }}>👥 Purchase team — what each person did</h3>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "0 0 10px" }}>
          Files forwarded = vendor stock files this person sent to the bot on the vendor's behalf.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Role</th>
                <th>Stock files forwarded</th>
                <th>Vendors covered</th>
                <th>Active days</th>
                <th>Messages to bot</th>
              </tr>
            </thead>
            <tbody>
              {data.team.length === 0 ? (
                <tr><td colSpan={6}>No team activity in this period.</td></tr>
              ) : (
                data.team.map((t) => (
                  <tr key={t.number}>
                    <td><strong>{t.name}</strong><div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>+{t.number}</div></td>
                    <td style={{ fontSize: "0.82rem" }}>{t.roles.join(", ")}</td>
                    <td>
                      {t.files_forwarded}
                      {t.failed_files ? <span style={{ color: "var(--color-danger)", fontSize: "0.78rem" }}> · {t.failed_files} unreadable</span> : null}
                    </td>
                    <td title={t.vendors.join(", ")}>
                      {t.vendors_covered}
                      {t.vendors.length ? <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>{t.vendors.slice(0, 4).join(", ")}{t.vendors.length > 4 ? " …" : ""}</div> : null}
                    </td>
                    <td>{t.active_days}</td>
                    <td>{t.messages_to_bot}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <h3 style={{ fontSize: "1rem", margin: "0 0 10px" }}>🤖 The bot</h3>
        <div className="stat-grid">
          <Stat label="Stock requests sent" value={bot.stock_requests} hint={`daily at ${data.request_time}`} />
          <Stat label="Reminders sent" value={bot.stock_reminders} hint={`at ${data.reminder_time} to vendors who had not sent`} />
          <Stat label="Files processed" value={bot.files_imported} hint={`${bot.files_failed} unreadable`} />
          <Stat
            label="Advance-order enquiries"
            value={bot.enquiries_sent ?? 0}
            hint={enquiryReply === null ? "none" : `${bot.enquiries_answered} answered (${enquiryReply}%) · ${bot.enquiries_unanswered} no reply · ${bot.advance_orders} orders`}
          />
          <Stat
            label="WhatsApp messages"
            value={bot.messages_sent}
            hint={`sent · ${bot.messages_failed} failed · ${bot.messages_received} received`}
          />
          <Stat
            label="Dealer Portal uploads"
            value={Object.values(bot.portal_uploads || {}).reduce((a, b) => a + b, 0)}
            hint={Object.entries(bot.portal_uploads || {}).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(" · ") || "none"}
          />
        </div>
      </section>
      </>
      )}
    </Layout>
  );
}

function whoLabel(who) {
  if (who === "vendor itself") return "🙋 vendor";
  if (who === "website / e-mail") return "🌐 web";
  return `👤 ${who}`;
}

function DailyView({ data, openDay, setOpenDay }) {
  const registered = data.vendors.filter((v) => v.registered);
  const days = data.daily;
  return (
    <>
      <section className="panel">
        <h3 style={{ fontSize: "1rem", margin: "0 0 4px" }}>📅 Day by day</h3>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "0 0 10px" }}>
          Click a day to see which vendors sent stock (time, who sent it) and which did not. Vendors = registered outside
          vendors the bot asks every day; warehouses are counted separately.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Day</th>
                <th>Vendors sent</th>
                <th>On time</th>
                <th>Files</th>
                <th>Who sent the files</th>
                <th>Bot asked</th>
                <th>Enquiries</th>
                <th>Dealer Portal</th>
              </tr>
            </thead>
            <tbody>
              {days.map((d) => {
                const pct = d.vendors_asked ? Math.round((100 * d.vendors_sent) / d.vendors_asked) : 0;
                const open = openDay === d.date;
                return [
                  <tr key={d.date} onClick={() => setOpenDay(open ? null : d.date)} style={{ cursor: "pointer", opacity: d.sunday ? 0.6 : 1 }}>
                    <td>
                      <strong>{open ? "▾" : "▸"} {d.weekday} {d.date.slice(8, 10)}/{d.date.slice(5, 7)}</strong>
                      {d.sunday ? <div style={{ fontSize: "0.75rem" }}>Sunday</div> : null}
                    </td>
                    <td>
                      <Bar value={pct} />
                      <div style={{ fontSize: "0.78rem", color: "var(--color-text-muted)" }}>
                        {d.vendors_sent} of {d.vendors_asked}{d.warehouses_sent ? ` · +${d.warehouses_sent} warehouse` : ""}
                      </div>
                    </td>
                    <td>{d.vendors_sent ? `${d.on_time} / ${d.vendors_sent}` : "—"}</td>
                    <td>
                      {d.files}
                      {d.failed_files ? <span style={{ color: "var(--color-danger)", fontSize: "0.78rem" }}> · {d.failed_files} unreadable</span> : null}
                    </td>
                    <td style={{ fontSize: "0.82rem" }}>
                      {d.senders.length ? d.senders.map((x) => <div key={x.who}>{whoLabel(x.who)} · {x.files}</div>) : "—"}
                    </td>
                    <td style={{ fontSize: "0.82rem" }}>{d.bot_requests + d.bot_reminders ? `${d.bot_requests} req · ${d.bot_reminders} rem` : "—"}</td>
                    <td style={{ fontSize: "0.82rem" }}>{d.enquiries ? `${d.enquiries_answered}/${d.enquiries} answered` : "—"}</td>
                    <td style={{ fontSize: "0.82rem" }}>
                      {d.portal_ok + d.portal_failed ? `${d.portal_ok} ok${d.portal_failed ? ` · ${d.portal_failed} failed` : ""}` : "—"}
                    </td>
                  </tr>,
                  open ? (
                    <tr key={`${d.date}-detail`}>
                      <td colSpan={8} style={{ background: "var(--color-neutral-bg)" }}>
                        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
                          <div style={{ minWidth: 260 }}>
                            <strong>✅ Sent stock ({d.vendors.length})</strong>
                            {d.vendors.length ? (
                              d.vendors.map((v) => (
                                <div key={v.vendor} style={{ fontSize: "0.85rem" }}>
                                  {v.time} · {v.vendor} · {whoLabel(v.who)}
                                  {!v.registered ? <span style={{ color: "var(--color-text-muted)" }}> (warehouse)</span> : null}
                                </div>
                              ))
                            ) : (
                              <div style={{ fontSize: "0.85rem" }}>nobody</div>
                            )}
                          </div>
                          <div style={{ minWidth: 260 }}>
                            <strong>❌ Did not send ({d.missing.length})</strong>
                            {d.missing.map((name) => (
                              <div key={name} style={{ fontSize: "0.85rem" }}>{name}</div>
                            ))}
                          </div>
                        </div>
                      </td>
                    </tr>
                  ) : null,
                ];
              })}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <h3 style={{ fontSize: "1rem", margin: "0 0 4px" }}>🗓️ Vendor × day</h3>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.85rem", margin: "0 0 10px" }}>
          ✓ time = stock arrived (hover for who sent it) · ✗ = nothing that day.
        </p>
        <div className="table-scroll">
          <table className="data-table" style={{ fontSize: "0.8rem" }}>
            <thead>
              <tr>
                <th style={{ position: "sticky", left: 0, background: "#f8fafc" }}>Vendor</th>
                {days.map((d) => (
                  <th key={d.date} style={{ textAlign: "center", opacity: d.sunday ? 0.5 : 1 }}>
                    {d.weekday.slice(0, 2)}
                    <br />
                    {d.date.slice(8, 10)}
                  </th>
                ))}
                <th>Days</th>
              </tr>
            </thead>
            <tbody>
              {registered.map((v) => (
                <tr key={v.vendor_id}>
                  <td style={{ position: "sticky", left: 0, background: "#fff", whiteSpace: "nowrap" }}>
                    <strong>{v.vendor.length > 26 ? `${v.vendor.slice(0, 26)}…` : v.vendor}</strong>
                  </td>
                  {days.map((d) => {
                    const hit = v.by_day[d.date];
                    let bg = d.sunday ? "transparent" : "var(--color-danger-bg)";
                    if (hit) bg = hit.time < data.reminder_time ? "var(--color-success-bg)" : "var(--color-warning-bg)";
                    return (
                      <td
                        key={d.date}
                        title={hit ? `${hit.time} · ${hit.who}` : "nothing"}
                        style={{ textAlign: "center", background: bg, color: hit ? "inherit" : "var(--color-danger)", whiteSpace: "nowrap" }}
                      >
                        {hit ? `✓ ${hit.time}` : d.sunday ? "" : "✗"}
                      </td>
                    );
                  })}
                  <td><strong>{v.days_sent}</strong></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p style={{ color: "var(--color-text-muted)", fontSize: "0.8rem", margin: "6px 0 0" }}>
          Green = before the {data.reminder_time} reminder · yellow = after it · red = no stock.
        </p>
      </section>
    </>
  );
}
