import { readdir, readFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sharedStatePath = resolve(frontendRoot, "tests/browser/state.ts");
const timers = new Set(["setTimeout", "setInterval", "waitForTimeout", "sleep", "setDefaultTimeout", "setDefaultNavigationTimeout"]);

export function checkBrowserTiming(source, filename) {
  const file = ts.createSourceFile(filename, source, ts.ScriptTarget.Latest, true);
  const problems = [];
  const aliases = new Set(timers);
  const contexts = new Set(["context"]);
  const schedulers = new Set(["scheduler"]);
  const sourcePath = resolve(frontendRoot, filename);
  const shared = sourcePath === sharedStatePath;
  let importsSharedTest = false;
  function report(node, message) {
    const { line, character } = file.getLineAndCharacterOfPosition(node.getStart(file));
    problems.push(`${filename}:${line + 1}:${character + 1}: ${message}`);
  }
  function memberName(node) {
    if (ts.isIdentifier(node) || ts.isStringLiteral(node)) return node.text;
    if (ts.isPropertyAccessExpression(node)) return node.name.text;
    if (ts.isElementAccessExpression(node) && node.argumentExpression) return memberName(node.argumentExpression);
    return "";
  }
  function contextValue(node) {
    if (ts.isAwaitExpression(node)) return contextValue(node.expression);
    if (ts.isIdentifier(node)) return contexts.has(node.text);
    return ts.isCallExpression(node) && ["context", "launchControlledProfile"].includes(memberName(node.expression));
  }
  function visit(node) {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      const clause = node.importClause;
      const bindings = clause?.namedBindings;
      const module = node.moduleSpecifier.text;
      if (module === "@playwright/test" && !shared && clause && !clause.isTypeOnly) {
        const valueBindings = !bindings || !ts.isNamedImports(bindings) || bindings.elements.some(item => !item.isTypeOnly);
        if (valueBindings) report(node, "Import test/expect from the shared browser state helper.");
      }
      if (bindings && ts.isNamedImports(bindings)) {
        for (const item of bindings.elements) {
          const original = (item.propertyName ?? item.name).text;
          if (timers.has(original)) aliases.add(item.name.text);
          if (original === "scheduler" && /^(?:node:)?timers\/promises$/.test(module)) schedulers.add(item.name.text);
          const importedPath = resolve(dirname(sourcePath), module);
          if ([sharedStatePath, sharedStatePath.slice(0, -3)].includes(importedPath) && original === "test") importsSharedTest = true;
        }
      }
    }
    if (ts.isVariableDeclaration(node) && node.initializer) {
      if (ts.isIdentifier(node.name) && aliases.has(memberName(node.initializer))) aliases.add(node.name.text);
      if (ts.isIdentifier(node.name) && contextValue(node.initializer)) contexts.add(node.name.text);
      if (ts.isObjectBindingPattern(node.name)) {
        for (const item of node.name.elements) {
          if (aliases.has(memberName(item.propertyName ?? item.name)) && ts.isIdentifier(item.name)) aliases.add(item.name.text);
        }
      }
    }
    if (ts.isBindingElement(node) && memberName(node.propertyName) === "context" && ts.isIdentifier(node.name)) contexts.add(node.name.text);
    if (ts.isParameter(node) && ts.isIdentifier(node.name) && node.type && ts.isTypeReferenceNode(node.type) && memberName(node.type.typeName) === "BrowserContext") contexts.add(node.name.text);
    if (ts.isCallExpression(node)) {
      const name = memberName(node.expression);
      const receiver = (ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression)) ? node.expression.expression : undefined;
      if (aliases.has(name) || name === "slow") report(node, "Wait on observable state or advance the controlled clock; no sleeps or per-test budgets.");
      if (receiver && ["bind", "call", "apply"].includes(name) && aliases.has(memberName(receiver))) report(node, "Timer aliases still depend on host time; use state waits or the controlled clock.");
      if (receiver && name === "wait" && schedulers.has(memberName(receiver))) report(node, "Scheduler waits depend on host time; use observable state.");
      if (name === "toPass") report(node, "Poll observable state instead of retrying an assertion block.");
      if (!shared && (["newContext", "launchPersistentContext", "launch"].includes(name) || (name === "newPage" && receiver && !contextValue(receiver)))) report(node, "Use the shared context fixture or launchControlledProfile so new contexts keep a controlled clock.");
      if (!shared && (ts.isPropertyAccessExpression(node.expression) || ts.isElementAccessExpression(node.expression)) && memberName(node.expression.expression) === "clock" && ["install", "pauseAt", "resume"].includes(name)) {
        report(node, "The shared page fixture owns clock installation and pausing; use setClockTime/runFor.");
      }
    }
    if ((ts.isPropertyAssignment(node) || ts.isShorthandPropertyAssignment(node)) && ["timeout", "actionTimeout", "navigationTimeout", "retries", "slowMo"].includes(memberName(node.name))) {
      report(node, "Keep failure deadlines and retry policy in shared Playwright configuration; test waits must observe state.");
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  if (filename.endsWith(".spec.ts") && !importsSharedTest) report(file, "Browser specs must use the shared browser state test fixture.");
  return problems;
}

async function checkDirectory(directory) {
  const problems = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) problems.push(...await checkDirectory(path));
    else if (/\.[cm]?[jt]sx?$/.test(entry.name)) problems.push(...checkBrowserTiming(await readFile(path, "utf8"), relative(frontendRoot, path)));
  }
  return problems;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const problems = await checkDirectory(resolve(frontendRoot, "tests/browser"));
  if (problems.length) {
    console.error(problems.join("\n"));
    process.exitCode = 1;
  } else console.log("Browser timing check passed.");
}
