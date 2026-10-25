<p align="center">
  <img src="ProxyWrangler_Logo_256.png" alt="ProxyWrangler logo" width="96" height="96">
</p>

<h1 align="center">ProxyWrangler</h1>

<p align="center">Lightweight silhouette proxies for dense environment layouts.</p>

![ProxyWrangler cover](ProxyWrangler_Cover.png)

**Version 1.1.1** · **Blender 4.2+ target** · **Runtime-tested in Blender 5.1.2**

ProxyWrangler is a Blender extension for arranging heavy scene assets through simplified mesh stand-ins. Generate proxies, dress the scene, and render production geometry at the proxy placements. Linked library assets use local geometry snapshots and editable local overrides for placement changes.

[Open the HTML user guide](ProxyWrangler_Guide.html)

## Features

- **Adjustable proxy detail:** collapse ratios from 0.1% to 100%, with a 3% default.
- **Quality presets:** Custom Ratio, Fast Layout, Balanced, Detailed Silhouette, and Adaptive.
- **Minimum triangle protection:** preset and adaptive modes raise the retention ratio for smaller assets.
- **Regenerate in place:** rebuild proxy geometry while preserving object names, placements, and production references.
- **Shared proxy meshes:** matching evaluated geometry and collapse settings can reuse one local mesh data-block.
- **Selected / All controls:** generate, regenerate, and restore individual assets or groups.
- **Render preflight:** detect missing targets, duplicate mappings, unavailable libraries, excluded production assets, and incompatible current-frame transforms.
- **Automatic render visibility:** use production assets during rendering, then return to the proxy viewport layout after completion or cancellation.
- **Linked asset workflows:** mesh objects, collection instances, nested mesh instances, and local library overrides.

## Installation

1. Locate the packaged extension file, `proxy_wrangler-1.1.1.zip`, and keep it zipped.
2. Open **Edit → Preferences** in Blender.
3. In the extensions or add-ons section, choose **Install from Disk** from its menu. The menu location can vary between Blender versions.
4. Select the extension ZIP, install it, and enable **ProxyWrangler**.
5. In the **3D Viewport**, press **N** and open the **ProxyWrangler** sidebar tab.

The panel is titled **ProxyWrangler Engine**. No separate Python packages are required. The extension requests file access to check whether linked asset libraries are available during render preflight.

## Quick start

1. Save your scene and switch to **Object Mode**.
2. Select mesh objects or collection-instance objects.
3. Choose a **Quality** mode and, where available, a **Collapse Ratio**.
4. Under **Generate**, click **Selected**.
5. Arrange the selected `PRXY_` objects with **G**, **R**, and **S**.
6. Use **Render Preflight** to check the scene, then **Preflight & Render** or **F12** to render.
7. Continue working with proxies after the render, or use **Restore Production** to commit the placements and remove successful proxies.

Proxies use solid viewport display with a wire overlay. Their materials are removed, and their simplified geometry is baked. Object placement transfers to production assets; proxy vertex edits and added modifiers do not transfer to the originals.

## Quality settings

Collapse Ratio represents the percentage of geometry the decimator aims to retain. Lower percentages usually produce lighter proxies with less shape detail. Actual output and viewport speed depend on topology and scene complexity.

| Mode | Retention target | Minimum triangle floor |
| --- | --- | --- |
| Custom Ratio | Artist-controlled, 0.1%–100%; default 3% | Disabled |
| Fast Layout | 1% | Enabled |
| Balanced | 3% | Enabled |
| Detailed Silhouette | 15% | Enabled |
| Adaptive | Artist-controlled ratio, increased for smaller assets | Enabled |

**Minimum Triangles** defaults to **64**. Assets already below the floor retain their geometry. The floor adjusts the decimation ratio; final triangle counts are approximate.

Changing a setting does not rebuild existing proxies automatically. Select the proxies and use **Regenerate → Selected** to apply the new quality without losing their placements. Quality settings are saved with the scene.

## Selected and All controls

| Action | Selected | All |
| --- | --- | --- |
| Generate | Selected eligible production assets | Eligible visible assets in the active view layer |
| Regenerate | Selected proxies or their selected production targets | Proxies in the active scene |
| Restore Production | Selected proxies or their selected production targets | Proxies in the active scene |

Generation skips assets that already have proxies. Regeneration and restoration use the view layer recorded during generation; switch to that layer if prompted. Assets that cannot be processed safely are retained and reported.

## Shared geometry

**Share Matching Geometry** is enabled by default. The extension compares evaluated geometry and decimation settings before reusing proxy mesh data. Separate proxy objects retain independent transforms and production references.

- Different evaluated deformations produce separate proxy meshes.
- Edited cached proxy geometry is excluded from reuse for new matches.
- Regenerating selected proxies does not replace unselected proxies’ mesh assignments.
- The panel shows the proxy count and unique mesh count.

Vertex edits to a shared mesh affect every proxy using that mesh, following Blender’s shared-data behavior. Disable sharing before regeneration if separate proxy mesh data is needed.

## Render preflight and visibility

### Checked rendering

Use one of these entry points to validate the scene before starting a render:

- **Render Preflight** checks without rendering.
- **Preflight & Render** starts a checked still render.
- **Preflight & Animation** starts a checked animation render.
- **F12** and **Ctrl+F12** are mapped to the checked render actions while the extension is enabled.
- The **Render** menu includes **ProxyWrangler Preflight & Render** and an animation equivalent.

Preflight blocks the checked render action if it detects a problem. Results appear under **Last preflight result** in the panel. If another keymap overrides the shortcuts, use the panel buttons.

### During and after rendering

Before rendering, proxies are hidden from the render and production assets are made visible at their proxy placements. After completion or cancellation, previous production transform values and render visibility flags are restored. Production assets are hidden again in the viewport, leaving the proxies available for layout work. A deferred visibility check runs after Blender’s render cleanup.

The extension enables **Lock Interface** while proxies are in use. It restores the previous setting after all proxies are manually restored, or when the extension is disabled.

### Preflight limits

Native Blender render actions, direct calls to `bpy.ops.render.render`, and render-farm commands can bypass the blocking preflight operator. Visibility handlers still run when the extension is enabled, but cannot reliably cancel an already-started render. For scripted checked rendering, call `bpy.ops.proxy_wrangler.render()` after configuring the scene.

Preflight validates placement at the **current frame**. It does not validate every frame of an animation or every possible compositor, sequencer, and render-engine configuration. Other collection and scene-level render restrictions still apply.

## Linked assets and production restoration

ProxyWrangler creates local proxy geometry without editing linked library mesh data. Direct linked objects may receive local library overrides; linked collection members may require a local override hierarchy. Collection-instance objects can be represented by a combined proxy of their evaluated mesh contents.

Local collection memberships and override editability may change to provide editable production placements. Keep linked libraries accessible when reopening saved layouts.

**Restore Production → Selected / All** copies proxy world transforms to the production objects, makes successful targets visible in the viewport, and removes their proxy objects and unused proxy mesh data. Failed restorations keep the proxy and report the reason.

Rendering performs a temporary swap. Manual restoration commits the placements. Restore production assets before disabling the extension if you want the scene to render independently of its handlers.

## Troubleshooting

| Problem | Action |
| --- | --- |
| Buttons are disabled | Switch to Object Mode. |
| Proxy silhouette is too rough | Increase quality and regenerate the selected proxies. |
| Ratio changes have no effect | Use Regenerate. Choose Custom Ratio or Adaptive to use the slider. |
| Production target is missing | Recover the original object relationship from a saved scene. Recreating an object with the same name does not restore a saved object reference. |
| Duplicate production mapping | Remove the unintended proxy duplicate. To dress multiple copies, duplicate production placements first, then generate a proxy for each. |
| Constraints or animation prevent placement | Resolve the conflicting production setup and rerun preflight. |
| Asset is shared across scenes | Use separate production placement objects or collection instances per scene. Their linked mesh data can still be shared. |
| Asset has a sheared world transform | Use a placement without shear, such as correcting a rotated child under a nonuniformly scaled parent. |
| Operation requests another view layer | Switch to the view layer used when generating the proxy. |
| Asset is missing from the render | Check the preflight report, library availability, camera, collection render visibility, and view-layer exclusions. |

## Compatibility and validation

The manifest targets **Blender 4.2 and later**. Runtime validation has been performed in **Blender 5.1.2**; Blender 4.2 has not been runtime-tested.

Validation covered local and linked meshes, collection hierarchies, nested instances, library overrides, shared geometry, regeneration, saved-file recovery, checked still and animation renders, cancellation-handler recovery, transform rollback, and extension registration under Blender’s restricted startup context.

## Build from source

With Blender available on your command path, run these commands from the repository root:

```sh
blender --background --factory-startup --command extension build --source-dir .
blender --background --factory-startup --command extension validate proxy_wrangler-1.1.1.zip
```

The installable archive contains `blender_manifest.toml` and `__init__.py`. The guide and branding images are repository documentation assets.

## License

The extension code is declared **GPL-3.0-or-later** in `blender_manifest.toml`.
