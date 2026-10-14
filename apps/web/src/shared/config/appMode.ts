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
function evaluate(): AppMode | ConfigError {
  try {
    return readAppMode(import.meta.env);
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
