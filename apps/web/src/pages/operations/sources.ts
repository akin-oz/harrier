import type { components } from "@harrier/contract";

export type SourceName = components["schemas"]["SourceName"];

// Every source the contract names, labelled. A `Record` over the generated
// union, so a source the domain adds is a type error here until it has a
// label, rather than an option the page silently lacks.
export const SOURCE_LABEL: Record<SourceName, string> = {
  greenhouse: "Greenhouse",
  ashby: "Ashby",
  lever: "Lever",
  remoteok: "RemoteOK",
  apify_linkedin: "LinkedIn (Apify)",
  wellfound: "Wellfound",
  wttj: "Welcome to the Jungle",
};

export function isSource(value: string): value is SourceName {
  return value in SOURCE_LABEL;
}
