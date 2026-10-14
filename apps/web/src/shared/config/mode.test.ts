import { expect, test } from "vitest";

import { ConfigError, readAppMode } from "./mode";

const HOSTED = {
  VITE_HARRIER_AUTH: "supabase",
  VITE_SUPABASE_URL: "https://abc.supabase.co",
  VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_synthetic",
  VITE_HARRIER_API_URL: "https://api.example.com",
};

function refusal(env: Record<string, string | undefined>): ConfigError {
  try {
    readAppMode(env);
  } catch (error) {
    if (error instanceof ConfigError) return error;
    throw error;
  }
  throw new Error("the configuration was accepted");
}

// Built in pieces, so no line of this file reads as a key to a secret scanner:
// it is a synthetic value, shaped like Supabase's secret keys and valid for
// nothing.
const SERVICE_SIDE = ["sb", "secret", "placeholder"].join("_");

// A JWT whose payload says it is the service role, built here and signed with
// nothing: only the payload is read.
function jwt(claims: Record<string, unknown>): string {
  const part = (value: unknown): string =>
    btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${part({ alg: "HS256" })}.${part(claims)}.synthetic-signature`;
}

test("local mode reads nothing but the switch", () => {
  expect(readAppMode({})).toEqual({ kind: "local" });
  expect(readAppMode({ VITE_HARRIER_AUTH: "" })).toEqual({ kind: "local" });
  // A stray hosted variable cannot make the local app half hosted.
  expect(
    readAppMode({ VITE_SUPABASE_URL: "http://not-even-https", VITE_HARRIER_API_URL: "nonsense" }),
  ).toEqual({ kind: "local" });
});

test("a complete hosted configuration is read as it is written", () => {
  expect(readAppMode(HOSTED)).toEqual({
    kind: "hosted",
    supabaseUrl: "https://abc.supabase.co",
    publishableKey: "sb_publishable_synthetic",
    apiUrl: "https://api.example.com",
  });
  // The legacy anon key is a JWT whose role is `anon`.
  const anon = jwt({ role: "anon" });
  expect(readAppMode({ ...HOSTED, VITE_SUPABASE_PUBLISHABLE_KEY: anon })).toMatchObject({
    publishableKey: anon,
  });
});

test("a hosted build refuses each bad variable", () => {
  const secretJwt = jwt({ role: "service_role" });
  const cases: {
    name: string;
    env: Record<string, string | undefined>;
    variable: string;
    text: string;
  }[] = [
    {
      name: "another switch value",
      env: { ...HOSTED, VITE_HARRIER_AUTH: "firebase" },
      variable: "VITE_HARRIER_AUTH",
      text: "VITE_HARRIER_AUTH must be empty or supabase",
    },
    {
      name: "no Supabase URL",
      env: { ...HOSTED, VITE_SUPABASE_URL: undefined },
      variable: "VITE_SUPABASE_URL",
      text: "VITE_SUPABASE_URL is required when VITE_HARRIER_AUTH=supabase",
    },
    {
      name: "a Supabase URL that is not https",
      env: { ...HOSTED, VITE_SUPABASE_URL: "http://abc.supabase.co" },
      variable: "VITE_SUPABASE_URL",
      text: "VITE_SUPABASE_URL must be an https:// origin with no path, query or fragment",
    },
    {
      name: "a Supabase URL with a path",
      env: { ...HOSTED, VITE_SUPABASE_URL: "https://abc.supabase.co/rest/v1" },
      variable: "VITE_SUPABASE_URL",
      text: "VITE_SUPABASE_URL must be an https:// origin with no path, query or fragment",
    },
    {
      name: "no key",
      env: { ...HOSTED, VITE_SUPABASE_PUBLISHABLE_KEY: "" },
      variable: "VITE_SUPABASE_PUBLISHABLE_KEY",
      text: "VITE_SUPABASE_PUBLISHABLE_KEY is required when VITE_HARRIER_AUTH=supabase",
    },
    {
      name: "no API URL",
      env: { ...HOSTED, VITE_HARRIER_API_URL: undefined },
      variable: "VITE_HARRIER_API_URL",
      text: "VITE_HARRIER_API_URL is required when VITE_HARRIER_AUTH=supabase",
    },
    {
      name: "an API URL that is not https",
      env: { ...HOSTED, VITE_HARRIER_API_URL: "http://api.example.com" },
      variable: "VITE_HARRIER_API_URL",
      text: "VITE_HARRIER_API_URL must be an https:// origin with no path, query or fragment",
    },
    {
      name: "an API URL with a path",
      env: { ...HOSTED, VITE_HARRIER_API_URL: "https://api.example.com/api" },
      variable: "VITE_HARRIER_API_URL",
      text: "VITE_HARRIER_API_URL must be an https:// origin with no path, query or fragment",
    },
    {
      name: "an API URL with a query",
      env: { ...HOSTED, VITE_HARRIER_API_URL: "https://api.example.com?x=1" },
      variable: "VITE_HARRIER_API_URL",
      text: "VITE_HARRIER_API_URL must be an https:// origin with no path, query or fragment",
    },
    {
      name: "an API URL with a fragment",
      env: { ...HOSTED, VITE_HARRIER_API_URL: "https://api.example.com#x" },
      variable: "VITE_HARRIER_API_URL",
      text: "VITE_HARRIER_API_URL must be an https:// origin with no path, query or fragment",
    },
    {
      name: "a secret key",
      env: { ...HOSTED, VITE_SUPABASE_PUBLISHABLE_KEY: SERVICE_SIDE },
      variable: "VITE_SUPABASE_PUBLISHABLE_KEY",
      text: "VITE_SUPABASE_PUBLISHABLE_KEY holds a secret key. A browser build takes the publishable key or the anon key only",
    },
    {
      name: "a service role key",
      env: { ...HOSTED, VITE_SUPABASE_PUBLISHABLE_KEY: secretJwt },
      variable: "VITE_SUPABASE_PUBLISHABLE_KEY",
      text: "VITE_SUPABASE_PUBLISHABLE_KEY holds a secret key. A browser build takes the publishable key or the anon key only",
    },
  ];
  for (const { name, env, variable, text } of cases) {
    const error = refusal(env);
    expect(error.variable, name).toBe(variable);
    expect(error.message, name).toBe(text);
    // No message holds a key's value.
    for (const value of [env.VITE_SUPABASE_PUBLISHABLE_KEY, secretJwt, SERVICE_SIDE]) {
      if (value !== undefined && value !== "") expect(error.message, name).not.toContain(value);
    }
  }
});
