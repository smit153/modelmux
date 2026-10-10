/** Render client config snippets from `shared/templates/config/<target>.json`. */

import { readFileSync } from "node:fs";
import path from "node:path";

import { CliError } from "./errors.ts";
import { sharedDir } from "./providers.ts";
import { repr } from "./text.ts";

export const KEY_ENV = "MODELMUX_API_KEY";
export const TARGETS = ["litellm", "openai-python", "langchain", "curl", "env"] as const;

/** Python's `json.dumps()` of a string (the key is URL-safe, so no escaping differs). */
const jsonString = (key: string): string => JSON.stringify(key);

// How each target refers to the key: [by reference, when revealed].
const KEY_STYLES: Record<string, readonly [string, (key: string) => string]> = {
  litellm: [`os.environ/${KEY_ENV}`, (key) => key],
  python: [`os.environ["${KEY_ENV}"]`, jsonString],
  shell: [`$${KEY_ENV}`, (key) => key],
  env: ["", (key) => key],
};
const PLACEHOLDER = /\{\{(\w+)\}\}/g;

export interface Template {
  readonly name: string;
  readonly description: string;
  readonly per: string;
  readonly keyStyle: string;
  readonly header: string;
  readonly entry: string;
  readonly footer: string;
}

export interface Target {
  readonly provider: string;
  readonly displayName: string;
  readonly baseUrl: string;
  readonly models: readonly string[];
}

export function loadTemplate(name: string): Template {
  const file = path.join(sharedDir(), "templates", "config", `${name}.json`);
  let template: Template;
  try {
    const data = JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
    const field = (key: string): string => {
      if (!(key in data)) throw new TypeError(key);
      return data[key] as string;
    };
    template = {
      name,
      description: field("description"),
      per: field("per"),
      keyStyle: field("key_style"),
      header: field("header"),
      entry: field("entry"),
      footer: field("footer"),
    };
  } catch {
    throw new CliError(`The ${repr(name)} config template is missing or invalid.`, {
      hint: "Please report this bug.",
    });
  }
  if (!["provider", "model"].includes(template.per) || !(template.keyStyle in KEY_STYLES)) {
    throw new CliError(`The ${repr(name)} config template is invalid.`, {
      hint: "Please report this bug.",
    });
  }
  for (const key of ["header", "entry", "footer"] as const) {
    if (typeof template[key] !== "string") {
      throw new CliError(`The ${repr(name)} config template is missing or invalid.`, {
        hint: "Please report this bug.",
      });
    }
  }
  return template;
}

function fill(text: string, values: Record<string, string>): string {
  return text.replace(PLACEHOLDER, (match, key: string) => {
    if (!Object.hasOwn(values, key)) {
      throw new CliError(`Unknown placeholder ${match} in a config template.`, {
        hint: "Please report this bug.",
      });
    }
    return values[key]!;
  });
}

/** `apiKey` null means: refer to $MODELMUX_API_KEY instead of the value. */
export function render(template: Template, targets: readonly Target[], apiKey: string | null): string {
  const [reference, literal] = KEY_STYLES[template.keyStyle]!;
  const key = apiKey === null ? reference : literal(apiKey);
  const parts = [fill(template.header, { key })];
  for (const target of targets) {
    const models = template.per === "model" ? target.models : target.models.slice(0, 1);
    for (const model of models) {
      parts.push(
        fill(template.entry, {
          provider: target.provider,
          PROVIDER: target.provider.toUpperCase().replaceAll("-", "_"),
          provider_var: target.provider.replaceAll("-", "_"),
          display_name: target.displayName,
          model,
          base_url: target.baseUrl,
          key,
        }),
      );
    }
  }
  parts.push(fill(template.footer, { key }));
  return parts.join("").replace(/\n+$/, "") + "\n";
}
