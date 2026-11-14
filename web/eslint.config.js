import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "src/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    files: ["scripts/**"],
    languageOptions: { globals: { process: "readonly", console: "readonly" } },
  },
  {
    rules: {
      // Evidence is untrusted text; React escaping is the only rendering path (ENGINEERING_PLAN).
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Never render untrusted text as HTML.",
        },
      ],
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Never render untrusted text as HTML." },
        { object: "window", property: "localStorage", message: "The token is memory-only." },
        { object: "window", property: "sessionStorage", message: "The token is memory-only." },
      ],
      "no-restricted-globals": ["error", "localStorage", "sessionStorage"],
    },
  },
  {
    // Tests have to look at storage and markup to prove the app never uses them.
    files: ["tests/**"],
    rules: { "no-restricted-properties": "off", "no-restricted-globals": "off" },
  },
);
