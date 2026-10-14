/**
 * Which harrier this build of the app is (spec 114).
 *
 * `VITE_HARRIER_AUTH=supabase` builds the hosted app; unset or empty builds
 * the local app of spec 035. Hosted needs three more variables, and this
 * function is the one place that reads them, so `vite.config.ts` can refuse a
 * bad hosted build before it is bundled and the app can refuse one at start.
 *
 * Local mode reads nothing else: a stray `VITE_SUPABASE_URL` cannot put the
 * local app into a half-hosted state.
 */

export type AppMode =
  | { readonly kind: "local" }
  | {
      readonly kind: "hosted";
      readonly supabaseUrl: string;
      readonly publishableKey: string;
      readonly apiUrl: string;
    };

export type Env = Readonly<Record<string, string | undefined>>;

/** A refused configuration. It names the variable and never a key's value. */
export class ConfigError extends Error {
  readonly variable: string;

  constructor(variable: string, message: string) {
    super(message);
    this.name = "ConfigError";
    this.variable = variable;
  }
}

const AUTH = "VITE_HARRIER_AUTH";
const SUPABASE_URL = "VITE_SUPABASE_URL";
const KEY = "VITE_SUPABASE_PUBLISHABLE_KEY";
const API_URL = "VITE_HARRIER_API_URL";

/** `https://host` and nothing after it: no path, query, fragment or login. */
function origin(env: Env, variable: string): string {
  const value = env[variable];
  if (value === undefined || value === "") {
    throw new ConfigError(variable, `${variable} is required when ${AUTH}=supabase`);
  }
  const refusal = new ConfigError(
    variable,
    `${variable} must be an https:// origin with no path, query or fragment`,
  );
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw refusal;
  }
  if (url.protocol !== "https:" || url.origin !== value) throw refusal;
  return value;
}

function jwtRole(key: string): string | null {
  const parts = key.split(".");
  const payload = parts[1];
  if (parts.length !== 3 || payload === undefined) return null;
  try {
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/");
    const claims: unknown = JSON.parse(atob(padded));
    if (typeof claims !== "object" || claims === null || !("role" in claims)) return null;
    return typeof claims.role === "string" ? claims.role : null;
  } catch {
    return null;
  }
}

/**
 * A secret key in a bundle is already leaked to everyone who loads the page,
 * so the build refuses one before it ships: Supabase's `sb_secret_` keys, and
 * the legacy `service_role` JWT. The two shapes are all this knows; a future
 * secret format would pass it (spec 114, Honest limitations). Refused in
 * either mode: Vite puts the key variable's value in the bundle whether or not
 * the local app reads it.
 */
function refuseSecretKey(env: Env): void {
  const value = env[KEY];
  if (value === undefined || value === "") return;
  if (value.startsWith("sb_secret_") || jwtRole(value) === "service_role") {
    throw new ConfigError(
      KEY,
      `${KEY} holds a secret key. A browser build takes the publishable key or the anon key only`,
    );
  }
}

/** The project's public client key. */
function publishableKey(env: Env): string {
  const value = env[KEY];
  if (value === undefined || value === "") {
    throw new ConfigError(KEY, `${KEY} is required when ${AUTH}=supabase`);
  }
  return value;
}

export function readAppMode(env: Env): AppMode {
  refuseSecretKey(env);
  const auth = env[AUTH] ?? "";
  if (auth === "") return { kind: "local" };
  if (auth !== "supabase") {
    throw new ConfigError(AUTH, `${AUTH} must be empty or supabase`);
  }
  return {
    kind: "hosted",
    supabaseUrl: origin(env, SUPABASE_URL),
    publishableKey: publishableKey(env),
    apiUrl: origin(env, API_URL),
  };
}
