// Kaleo's voice: a spoken digest of a finished run.
//
// `summarize` composes what gets said, `backends` knows how to say it,
// `speaker` makes sure only one thing is ever being said, and `settings`
// remembers whether to say anything at all.

export * from "./backends";
export * from "./settings";
export * from "./speaker";
export * from "./summarize";
