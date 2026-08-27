import { createForgeCatalog } from "/assets/forge/catalog.js";
import { createForgeModeration } from "/assets/forge/moderation.js";
import { createForgeStudio } from "/assets/forge/studio.js";

export function createForgeController({ loadPacks, loadSoulOptions }) {
  const catalog = createForgeCatalog();
  const moderation = createForgeModeration();
  const studio = createForgeStudio({
    loadPacks,
    loadSoulOptions,
    loadModeration: moderation.loadModeration,
  });

  function initialize() {
    catalog.initialize();
    studio.initialize();
  }

  return {
    initialize,
    loadForge: catalog.loadForge,
    loadStudio: studio.loadStudio,
  };
}
