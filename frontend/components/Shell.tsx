"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { ApiError, QueueItem, QueueStats, getToken, setToken, useApi } from "@/lib/api";

const LINKS = [
  { href: "/", label: "Waiting for you" },
  { href: "/reviews", label: "All reviews" },
  { href: "/costs", label: "Costs" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const queue = useApi<{ items: QueueItem[]; stats: QueueStats }>("/api/hitl", 15000);
  const me = useApi<{ role: string; name: string }>("/api/me");
  const current = (href: string) =>
    href === "/" ? pathname === "/" || pathname.startsWith("/queue") : pathname.startsWith(href);

  return (
    <div className="shell">
      <aside className="rail">
        <div className="product">
          PR review agent
          <small>Reviewer console</small>
        </div>
        <nav aria-label="Sections">
          {LINKS.map((link) => (
            <Link key={link.href} href={link.href} aria-current={current(link.href) ? "page" : undefined}>
              <span>{link.label}</span>
              {link.href === "/" && queue.data ? <span className="count">{queue.data.stats.pending}</span> : null}
            </Link>
          ))}
        </nav>
        <div className="foot">
          {me.data ? (
            <>
              Signed in as {me.data.name} ({me.data.role}).{" "}
              {getToken() ? (
                <button className="quiet" onClick={() => setToken("")}>
                  Sign out
                </button>
              ) : null}
            </>
          ) : null}
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

/** Shows an API problem in the interface's own words, with the way out. */
export function Problem({ error }: { error: ApiError | null }) {
  const [token, setDraft] = useState("");
  if (!error) return null;
  if (error.status === 401) {
    return (
      <form
        className="notice"
        onSubmit={(e) => {
          e.preventDefault();
          setToken(token.trim());
        }}
      >
        <label htmlFor="token">
          API token <span className="hint">One of the tokens in the server's API_TOKENS setting.</span>
        </label>
        <input id="token" type="password" value={token} onChange={(e) => setDraft(e.target.value)} autoComplete="off" />
        <div className="actions">
          <button className="primary" type="submit" disabled={!token.trim()}>
            Sign in
          </button>
        </div>
      </form>
    );
  }
  if (error.status === 403) {
    return (
      <div className="notice problem">
        <p>Your token does not allow this. Ask an admin for a reviewer token.</p>
      </div>
    );
  }
  return (
    <div className="notice problem">
      <p>{error.status === 0 ? error.message : `The API answered ${error.status}: ${error.message}`}</p>
    </div>
  );
}
