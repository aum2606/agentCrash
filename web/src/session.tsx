import { createContext, useContext } from "react";

import type { Api } from "./api/client";

export interface Session {
  api: Api;
  /** Forget the token (it only ever lives in React state). */
  signOut: () => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("No session");
  return session;
}
