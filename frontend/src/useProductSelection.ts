import { useEffect, useMemo, useState } from "react";

import type {
  DriverDescriptor,
  ProductProfileRecord,
  ProductSiteOverview,
} from "./types";

export type DriverChoice = {
  driverId: string;
  testingContext: string;
  prodContext: string;
  previewContext: string;
  label: string;
  driverLabel: string;
  repository?: string;
};

const DEFAULT_CHOICES: DriverChoice[] = [
  {
    driverId: "sellyouroutboard",
    testingContext: "sellyouroutboard-testing",
    prodContext: "sellyouroutboard-testing",
    previewContext: "sellyouroutboard-testing",
    label: "SellYourOutboard",
    driverLabel: "generic-web",
  },
  {
    driverId: "verireel",
    testingContext: "verireel",
    prodContext: "verireel",
    previewContext: "verireel-testing",
    label: "VeriReel",
    driverLabel: "verireel",
  },
  {
    driverId: "odoo",
    testingContext: "cm",
    prodContext: "cm",
    previewContext: "",
    label: "Odoo CM",
    driverLabel: "odoo",
  },
  {
    driverId: "odoo",
    testingContext: "opw",
    prodContext: "opw",
    previewContext: "",
    label: "Odoo OPW",
    driverLabel: "odoo",
  },
];

const PRODUCT_ENVIRONMENT_ALIASES: Record<"testing" | "prod", Set<string>> = {
  testing: new Set(["testing", "test", "staging", "stage", "qa"]),
  prod: new Set(["prod", "production", "stable", "live"]),
};

export function choiceKey(choice: DriverChoice): string {
  return `${choice.driverId}:${choice.testingContext}:${choice.prodContext}:${choice.previewContext}`;
}

function choiceDisplayKey(choice: DriverChoice): string {
  const normalizedLabel = choice.label.trim().toLowerCase();
  return `${choice.driverId}:${normalizedLabel}`;
}

function labelForDriverContext(driver: DriverDescriptor, context: string): string {
  if (driver.driver_id === "odoo") {
    if (context === "cm") {
      return "Odoo CM";
    }
    if (context === "opw") {
      return "Odoo OPW";
    }
  }
  return driver.label;
}

function displayNameForProduct(profile: ProductProfileRecord): string {
  if (profile.product === "sellyouroutboard") {
    return "SellYourOutboard";
  }
  return profile.display_name || profile.product;
}

function contextForProductLane(
  profile: ProductProfileRecord,
  instance: "testing" | "prod",
): string {
  const lane = profile.lanes.find(
    (candidate) => candidate.instance === instance,
  );
  if (lane?.context.trim()) {
    return lane.context.trim();
  }
  const fallbackLane = profile.lanes.find((candidate) =>
    candidate.context.trim(),
  );
  return fallbackLane?.context.trim() || "";
}

function choiceFromProductProfile(profile: ProductProfileRecord): DriverChoice {
  return {
    driverId: profile.product,
    testingContext: contextForProductLane(profile, "testing"),
    prodContext: contextForProductLane(profile, "prod"),
    previewContext: profile.preview?.enabled
      ? profile.preview.context.trim()
      : "",
    label: displayNameForProduct(profile),
    driverLabel: profile.driver_id,
    repository: profile.repository,
  };
}

function normalizedProductEnvironment(value: string): string {
  return value.trim().toLowerCase().replaceAll("_", "-");
}

function contextForProductEnvironment(
  product: ProductSiteOverview,
  environment: "testing" | "prod",
  reservedContext = "",
): string {
  const summaries = product.environments
    .map((summary) => ({
      environment: normalizedProductEnvironment(summary.environment),
      context: summary.context.trim(),
    }))
    .filter((summary) => summary.context);
  const aliases = PRODUCT_ENVIRONMENT_ALIASES[environment];
  const summary = summaries.find((candidate) =>
    aliases.has(candidate.environment),
  );
  if (summary) {
    return summary.context;
  }
  const distinctFallback = summaries.find(
    (candidate) => candidate.context !== reservedContext,
  );
  return distinctFallback?.context ?? summaries[0]?.context ?? "";
}

export function choiceFromProductOverview(
  product: ProductSiteOverview,
): DriverChoice {
  const prodContext = contextForProductEnvironment(product, "prod");
  const testingContext = contextForProductEnvironment(
    product,
    "testing",
    prodContext,
  );
  return {
    driverId: product.product,
    testingContext,
    prodContext,
    previewContext: product.preview.enabled
      ? product.preview.context.trim()
      : "",
    label: product.display_name || product.product,
    driverLabel: product.driver_id,
    repository: product.repository,
  };
}

export function useProductSelection(
  {
    drivers,
    productProfiles,
    productOverviews,
  }: {
    drivers: DriverDescriptor[];
    productProfiles: ProductProfileRecord[];
    productOverviews: ProductSiteOverview[];
  },
) {
  const [selected, setSelected] = useState<DriverChoice>(DEFAULT_CHOICES[0]);
  const choices = useMemo(() => {
    const driverChoices: DriverChoice[] = drivers.flatMap((driver) => {
      const stableContexts = driver.context_patterns.filter((context) => {
        return context !== "verireel-testing" && !context.includes("*");
      });
      return stableContexts.map((context) => ({
        driverId: driver.driver_id,
        testingContext: context,
        prodContext: context,
        previewContext:
          driver.driver_id === "verireel" && context === "verireel"
            ? "verireel-testing"
            : "",
        label: labelForDriverContext(driver, context),
        driverLabel: driver.driver_id,
      }));
    });
    const profileChoices: DriverChoice[] = productProfiles.map((profile) =>
      choiceFromProductProfile(profile),
    );
    const overviewChoices: DriverChoice[] = productOverviews.map((overview) =>
      choiceFromProductOverview(overview),
    );
    const merged: DriverChoice[] = [
      ...overviewChoices,
      ...profileChoices,
      ...driverChoices,
      ...DEFAULT_CHOICES,
    ];
    const productDisplayKeys = new Set(
      [...overviewChoices, ...profileChoices, ...DEFAULT_CHOICES].map(
        choiceDisplayKey,
      ),
    );
    const seen = new Set<string>();
    return merged.filter((choice) => {
      const displayKey = choiceDisplayKey(choice);
      const key = productDisplayKeys.has(displayKey)
        ? displayKey
        : choiceKey(choice);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
  }, [drivers, productProfiles, productOverviews]);

  useEffect(() => {
    if (!choices.length) {
      return;
    }
    const selectedKey = choiceKey(selected);
    const selectedChoice = choices.find(
      (choice) => choiceKey(choice) === selectedKey,
    );
    const productBackedChoice = choices.find(
      (choice) => choice.driverId === selected.driverId && choice.repository,
    );
    if (
      productBackedChoice &&
      choiceKey(productBackedChoice) !== selectedKey &&
      !selectedChoice?.repository
    ) {
      setSelected(productBackedChoice);
      return;
    }
    if (selectedChoice) {
      return;
    }
    setSelected(choices[0]);
  }, [choices, selected]);

  return { choices, selected, setSelected };
}
