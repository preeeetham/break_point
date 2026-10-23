import { useState } from "react";
import { Link } from "react-router";
import { PlanActions } from "../components";
import { useLiveData } from "../live";
import type { Incident } from "../types";
import { until } from "../util";

export default function Approvals() {
  const { data } = useLiveData<Incident[]>("/api/approvals");
  const [perm, setPerm] = useState(typeof Notification === "undefined" ? "unsupported" : Notification.permission);
  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Approvals</h1>
          <p className="sub">Plans waiting for a human. Nothing changes until one is approved.</p>
        </div>
        {perm === "default" && (
          <button className="btn small quiet" onClick={() => Notification.requestPermission().then(setPerm)}>
            Notify me in the browser
          </button>
        )}
      </div>
      {data && !data.length ? (
        <div className="card empty"><h2>Nothing to decide</h2><p>New plans appear here and in the war room.</p></div>
      ) : (
        <div className="stack">
          {(data ?? []).map((inc) => (
            <section key={inc.id} className="card stack-s">
              <div className="spread">
                <div className="row"><span className="mono faint">{inc.id}</span><strong>{inc.title}</strong></div>
                <span className="small faint">escalates {until(inc.approval.expires_at)}</span>
              </div>
              {inc.diagnosis && <p className="muted">{inc.diagnosis.summary}</p>}
              <PlanActions inc={inc} />
              <div><Link className="btn primary" to={`/incidents/${inc.id}/plan`}>Review and decide</Link></div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
