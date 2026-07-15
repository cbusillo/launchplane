import * as nodeModule from "node:module";

if (typeof nodeModule.registerHooks === "function") {
  nodeModule.registerHooks({
    resolve(specifier, context, nextResolve) {
      try {
        return nextResolve(specifier, context);
      } catch (error) {
        if (
          error?.code !== "ERR_MODULE_NOT_FOUND" ||
          !specifier.startsWith(".") ||
          /\.[a-z0-9]+$/i.test(specifier)
        ) {
          throw error;
        }
        return nextResolve(`${specifier}.ts`, context);
      }
    },
  });
} else {
  process.noDeprecation = true;
  nodeModule.register("./resolve-typescript.mjs", import.meta.url);
}
