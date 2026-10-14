import { ConfigError, readAppMode } from "./mode";
import type { AppMode } from "./mode";

/**
 * This build's mode, read once from the variables Vite put in the bundle.
 *
 * A bad hosted configuration is held as an error rather than thrown at import,
 * so the app can render its misconfiguration page and make no request
 * (spec 114). Everything that talks to a server asks `appMode()`, which
 * throws the same error, so nothing can run on a half-read configuration.
 */
// Vite types a variable as `any`; the build puts a string there or nothing.
function text(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function evaluate(): AppMode | ConfigError {
  try {
    // The four variables by name, never `import.meta.env` as a whole: Vite
    // copies a whole `import.meta.env` into the bundle, with every `VITE_`
    // variable in the build's environment, secret or not.
    return readAppMode({
      VITE_HARRIER_AUTH: text(import.meta.env.VITE_HARRIER_AUTH),
      VITE_SUPABASE_URL: text(import.meta.env.VITE_SUPABASE_URL),
      VITE_SUPABASE_PUBLISHABLE_KEY: text(import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY),
      VITE_HARRIER_API_URL: text(import.meta.env.VITE_HARRIER_API_URL),
    });
  } catch (error) {
    if (error instanceof ConfigError) return error;
    throw error;
  }
}

const evaluated = evaluate();

/** The configuration error of a misconfigured build, or null. */
export function configError(): ConfigError | null {
  return evaluated instanceof ConfigError ? evaluated : null;
}

export function appMode(): AppMode {
  if (evaluated instanceof ConfigError) throw evaluated;
  return evaluated;
}
