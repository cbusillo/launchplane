// Print the text the frontend shows people, one JSON object per line, for
// scripts/validate_role_words.py. JSX text counts, and so do string literals
// that are a phrase or a capitalized word, or the value of an attribute such as
// aria-label or title. Lowercase keys, class names, element ids, module paths,
// types, and comments do not. A literal that a reader
// parses, such as a marker other repositories write, keeps its legacy spelling
// when its line or the line above says "role-words: legacy".
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const SOURCE = join(FRONTEND, "src");
const PHRASE = /\S\s+\S|^[A-Z][a-z]/;
const LEGACY = "role-words: legacy";
const HIDDEN_ATTRIBUTE = /^(?:className|id|htmlFor|key|data-.*)$/;
const SHOWN_ATTRIBUTE = /^(?:aria-label|aria-description|title|placeholder|alt|label)$/;

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

function jsxAttributeName(node) {
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isJsxAttribute(current)) return current.name.getText();
    if (ts.isJsxElement(current) || ts.isJsxSelfClosingElement(current) || ts.isStatement(current)) {
      return null;
    }
  }
  return null;
}

function isNotShown(node) {
  const parent = node.parent;
  if (!parent) return false;
  if (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent)) return true;
  if (HIDDEN_ATTRIBUTE.test(jsxAttributeName(node) ?? "")) return true;
  return ts.isLiteralTypeNode(parent);
}

export function visibleStrings(fileName, text) {
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true);
  const lines = text.split("\n");
  const found = [];
  const visit = (node) => {
    let value = null;
    let always = false;
    if (ts.isJsxText(node)) {
      value = node.text;
      always = true;
    } else if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node)
    ) {
      value = isNotShown(node) ? null : node.text;
      always = ts.isJsxAttribute(node.parent) && SHOWN_ATTRIBUTE.test(node.parent.name.getText());
    }
    if (value !== null && value.trim() && (always || PHRASE.test(value.trim()))) {
      const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
      if (!`${lines[line - 1] ?? ""}\n${lines[line]}`.includes(LEGACY)) {
        found.push({ line: line + 1, text: value.replace(/\s+/g, " ").trim() });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  for (const path of sourceFiles(SOURCE).sort()) {
    const file = relative(join(FRONTEND, ".."), path);
    for (const entry of visibleStrings(path, readFileSync(path, "utf8"))) {
      process.stdout.write(`${JSON.stringify({ file, ...entry })}\n`);
    }
  }
}
