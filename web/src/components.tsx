import type { ReactNode } from "react";

import { type ApiProblem } from "./api/client";
import { useSession } from "./session";

/** Status as text first; colour only reinforces it. */
export function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value.replace(/[^a-z-]/g, "")}`}>
      <span aria-hidden="true">● </span>
      {value}
    </span>
  );
}

export function Problem({ error, retry }: { error: ApiProblem; retry?: () => void }) {
  const { signOut } = useSession();
  const heading =
    error.kind === "auth"
      ? "The token was rejected"
      : error.kind === "network"
        ? "The local API is not available"
        : "The request was refused";
  return (
    <div role="alert" className="problem">
      <strong>{heading}</strong>
      <p>
        {error.code}: {error.message}
      </p>
      {error.details.length > 0 && (
        <ul>
          {error.details.map((d, i) => (
            <li key={i}>{Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(", ")}</li>
          ))}
        </ul>
      )}
      {error.kind === "auth" && <button onClick={signOut}>Enter a different token</button>}
      {error.kind !== "auth" && retry && <button onClick={retry}>Try again</button>}
    </div>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export function Pair({ name, children }: { name: string; children: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{children}</dd>
    </>
  );
}
