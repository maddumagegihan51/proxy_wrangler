import hashlib
import logging
import math
from array import array
from itertools import groupby
from pathlib import Path

import bmesh
import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from mathutils import Matrix


AUTHOR = "Gihan Nirmal Maddumage"
LOG = logging.getLogger(__package__ or "proxy_wrangler")
RATIO = 0.03
RENDER_STATES = {}
VIEWPORT_RESTORE_SCENES = {}
KEYMAPS = []
TRANSFORM_FIELDS = (
    "location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale",
    "delta_location", "delta_rotation_euler", "delta_rotation_quaternion", "delta_scale",
)
SCOPE_ITEMS = [('SELECTED', "Selected", "Use selected assets"),
               ('ALL', "All", "Use all proxies; generation uses eligible visible assets in the active view layer")]


def _working_proxies(context, scope):
    selected = set(context.selected_objects)
    result = []
    for proxy in context.scene.objects:
        if not proxy.get("is_proxy"):
            continue
        target = proxy.get("_pw_target")
        if scope == 'ALL' or proxy in selected or (isinstance(target, bpy.types.Object) and target in selected):
            result.append(proxy)
    return result


def _capture_transform(obj):
    return {name: tuple(getattr(obj, name)) for name in TRANSFORM_FIELDS}


def _restore_transform(obj, values):
    for name, value in values.items():
        setattr(obj, name, value)


def _matrix_close(actual, desired):
    return all(math.isclose(actual[r][c], desired[r][c], rel_tol=1e-6, abs_tol=1e-5)
               for r in range(4) for c in range(4))


def _proxy_target_counts(scene):
    counts = {}
    for proxy in scene.objects:
        if proxy.get("is_proxy"):
            try:
                target = _target(proxy, scene)
                counts[target] = counts.get(target, 0) + 1
            except (ReferenceError, RuntimeError, TypeError):
                continue
    return counts


def _validate_proxy(proxy, scene, counts):
    if proxy.type != 'MESH' or proxy.library or not proxy.is_editable or proxy.users_scene != (scene,):
        raise RuntimeError("Proxy must be an editable mesh belonging exclusively to this scene")
    target = _target(proxy, scene)
    if target.type != 'MESH' and not (target.instance_type == 'COLLECTION' and target.instance_collection):
        raise RuntimeError("Production asset has no supported mesh or collection instance")
    if target.users_scene != (scene,):
        raise RuntimeError("Production asset is shared across scenes; use a separate placement per scene")
    if counts.get(target, 0) > 1:
        raise RuntimeError("Duplicate production mapping; remove the unintended proxy duplicate first")
    return target


def _mesh_digest(mesh):
    digest = hashlib.sha256()
    for values, attribute, width, code in (
        (mesh.vertices, "co", 3, 'f'),
        (mesh.edges, "vertices", 2, 'i'),
        (mesh.loops, "vertex_index", 1, 'i'),
        (mesh.polygons, "loop_start", 1, 'i'),
        (mesh.polygons, "loop_total", 1, 'i'),
        (mesh.polygons, "use_smooth", 1, 'b'),
        (mesh.edges, "use_edge_sharp", 1, 'b'),
    ):
        buffer = array(code, [0]) * (len(values) * width)
        values.foreach_get(attribute, buffer)
        digest.update(str((attribute, len(values))).encode())
        digest.update(buffer.tobytes())
    return digest.hexdigest()


def _proxy_cache(scene):
    cache = {}
    checked = set()
    if not scene.proxy_wrangler_share_geometry:
        return cache
    for obj in scene.objects:
        if not obj.get("is_proxy") or obj.type != 'MESH' or obj.data in checked:
            continue
        mesh = obj.data
        checked.add(mesh)
        key = mesh.get("_pw_cache_key")
        if key and mesh.library is None and mesh.shape_keys is None:
            if mesh.get("_pw_output_hash") == _mesh_digest(mesh):
                cache[key] = mesh
    return cache


def _quality_ratio(scene, mesh):
    triangles = sum(max(0, polygon.loop_total - 2) for polygon in mesh.polygons)
    mode = scene.proxy_wrangler_quality
    ratio = {'FAST': 0.01, 'BALANCED': 0.03, 'DETAILED': 0.15}.get(
        mode, scene.proxy_wrangler_collapse_percentage / 100.0
    )
    if mode != 'CUSTOM' and triangles:
        ratio = max(ratio, min(1.0, scene.proxy_wrangler_min_triangles / triangles))
    return min(1.0, max(0.001, ratio)), triangles


def _render_reachable(scene):
    reachable = set()
    for view_layer in scene.view_layers:
        if not view_layer.use:
            continue
        pending = [view_layer.layer_collection]
        while pending:
            layer = pending.pop()
            if layer.exclude or layer.collection.hide_render:
                continue
            reachable.update(layer.collection.objects)
            pending.extend(layer.children)
    return reachable


def _asset_dependencies(target, checked):
    pending = [target]
    visited = set()
    while pending:
        data = pending.pop()
        if data is None or data.as_pointer() in visited or data.as_pointer() in checked:
            continue
        visited.add(data.as_pointer())
        yield data
        if isinstance(data, bpy.types.Collection):
            pending.extend(data.children)
            pending.extend(data.objects)
        elif isinstance(data, bpy.types.Object):
            pending.extend([data.data, data.instance_collection])
            if data.override_library:
                pending.append(data.override_library.reference)
        elif isinstance(data, bpy.types.Mesh):
            pending.extend(data.materials)


def _preflight_static(scene):
    errors = []
    targets = {}
    reachable = _render_reachable(scene)
    checked = set()
    libraries = {}
    for proxy in scene.objects:
        if not proxy.get("is_proxy"):
            continue
        try:
            if proxy.type != 'MESH' or not _writable(proxy, "hide_render"):
                raise RuntimeError("Proxy must be an editable local mesh")
            if proxy.users_scene != (scene,):
                raise RuntimeError("Proxy is shared by multiple scenes")
            target = _target(proxy, scene)
            if target.type != 'MESH' and not (target.instance_type == 'COLLECTION' and target.instance_collection):
                raise RuntimeError("Production asset has no supported mesh or collection instance")
            if target.users_scene != (scene,):
                raise RuntimeError("Production asset is shared across scenes; use a separate placement per scene")
            if target == proxy or target.get("is_proxy"):
                raise RuntimeError("Production reference points to a proxy")
            if target in targets:
                raise RuntimeError(f"Duplicate production mapping with {targets[target]}")
            targets[target] = proxy.name
            if not _writable(target, "hide_render") or not _writable(target, "matrix_world"):
                raise RuntimeError("Production visibility or placement is read-only")
            if target not in reachable:
                raise RuntimeError("Production asset is excluded or its collection is hidden for rendering")
            if not all(math.isfinite(value) for row in proxy.matrix_world for value in row):
                raise RuntimeError("Proxy placement contains non-finite values")
            for data in _asset_dependencies(target, checked):
                if getattr(data, "is_missing", False):
                    raise RuntimeError(f"Missing linked data: {data.name}")
                if data.library:
                    path = bpy.path.abspath(data.library.filepath)
                    if path not in libraries:
                        libraries[path] = Path(path).is_file()
                    if not libraries[path]:
                        raise RuntimeError(f"Linked library is unavailable: {data.library.name}")
                checked.add(data.as_pointer())
            if target.instance_collection and target.instance_collection.hide_render:
                raise RuntimeError("Instanced collection is hidden for rendering")
        except (ReferenceError, RuntimeError, TypeError, OSError) as exc:
            errors.append(f"{proxy.name}: {exc}")
    return errors


def _preflight(scene):
    if bpy.app.is_job_running('RENDER') or RENDER_STATES:
        return ["Wait for the current render to finish"]
    errors = _preflight_static(scene)
    if scene.camera is None and any(obj.get("is_proxy") for obj in scene.objects):
        errors.append("Set an active render camera")
    if not errors:
        try:
            _render_prepare(scene)
            warning = scene.get("_pw_render_warning", "")
            if warning:
                errors.append(warning)
        finally:
            _render_restore(scene)
            _finish_viewport_restore()
    scene["_pw_preflight_report"] = "\n".join(errors) if errors else "Ready to render"
    return errors


def _restore_proxy_viewports(scene):
    for proxy in scene.objects:
        if not proxy.get("is_proxy"):
            continue
        try:
            target = _target(proxy, scene)
            if target.users_scene != (scene,):
                continue
            if _writable(target, "hide_viewport"):
                target.hide_viewport = True
            elif target.library is None and target.is_editable:
                layer_name = proxy.get("_pw_view_layer")
                layers = [scene.view_layers.get(layer_name)] if layer_name else scene.view_layers
                for layer in layers:
                    if layer and layer.objects.get(target.name) == target:
                        target.hide_set(True, view_layer=layer)
        except (ReferenceError, RuntimeError, TypeError):
            LOG.exception("Could not hide production asset in proxy layout")
    for layer in scene.view_layers:
        layer.update()


def _finish_viewport_restore():
    if bpy.app.is_job_running('RENDER'):
        return 0.1
    scenes = list(VIEWPORT_RESTORE_SCENES.values())
    VIEWPORT_RESTORE_SCENES.clear()
    for scene in scenes:
        try:
            if bpy.data.scenes.get(scene.name) == scene:
                _restore_proxy_viewports(scene)
        except ReferenceError:
            continue
    return None


def _arm_render_safety(scene):
    if "_pw_lock_interface" not in scene:
        scene["_pw_lock_interface"] = scene.render.use_lock_interface
    scene.render.use_lock_interface = True


def _disarm_render_safety(scene):
    if "_pw_lock_interface" in scene:
        scene.render.use_lock_interface = bool(scene["_pw_lock_interface"])
        del scene["_pw_lock_interface"]


def _depth(obj):
    depth = 0
    parent = obj.parent
    while parent:
        depth += 1
        parent = parent.parent
    return depth


@persistent
def _render_prepare(scene):
    if VIEWPORT_RESTORE_SCENES.pop(scene.as_pointer(), None) is not None:
        _restore_proxy_viewports(scene)
    proxies = [obj for obj in scene.objects if obj.get("is_proxy")]
    if not proxies:
        if "_pw_render_warning" in scene:
            del scene["_pw_render_warning"]
        return
    state = RENDER_STATES.setdefault(scene.as_pointer(), {
        "scene": scene, "objects": {}, "proxies": {},
    })
    errors = _preflight_static(scene)
    counts = _proxy_target_counts(scene)
    jobs = []
    for view_layer in scene.view_layers:
        view_layer.update()

    for proxy in proxies:
        try:
            if not _writable(proxy, "hide_render"):
                raise RuntimeError("Proxy render visibility is read-only")
            state["proxies"].setdefault(proxy, proxy.hide_render)
            proxy.hide_render = True
            target = _validate_proxy(proxy, scene, counts)
            if not _writable(target, "hide_render") or not _writable(target, "matrix_world"):
                raise RuntimeError("Production render visibility or placement is read-only")
            if target not in state["objects"]:
                state["objects"][target] = (
                    _capture_transform(target), target.hide_render, target.hide_viewport,
                    [(layer, target.hide_get(view_layer=layer)) for layer in scene.view_layers
                     if layer.objects.get(target.name) == target],
                )
            jobs.append((target, proxy.matrix_world.copy()))
        except Exception as exc:
            errors.append(f"{proxy.name}: {exc}")
            LOG.exception("Render preparation failed for %s", proxy.name)
    for unused_depth, group in groupby(sorted(jobs, key=lambda item: _depth(item[0])),
                                      key=lambda item: _depth(item[0])):
        batch = list(group)
        for target, desired in batch:
            try:
                if _writable(target, "hide_viewport"):
                    target.hide_viewport = False
                for layer, unused_hidden in state["objects"][target][3]:
                    target.hide_set(False, view_layer=layer)
                target.matrix_world = desired
                target.hide_render = False
            except Exception as exc:
                errors.append(f"{target.name}: {exc}")
                LOG.exception("Render placement failed for %s", target.name)
        for view_layer in scene.view_layers:
            view_layer.update()
        for target, desired in batch:
            actual = target.matrix_world
            if not _matrix_close(actual, desired):
                errors.append(f"{target.name}: Constraints or animation prevent exact render placement")
    if errors:
        scene["_pw_render_warning"] = "; ".join(errors)
    elif "_pw_render_warning" in scene:
        del scene["_pw_render_warning"]


@persistent
def _render_restore(scene):
    state = RENDER_STATES.pop(scene.as_pointer(), None)
    if not state:
        return
    for target, (transforms, hide_render, hide_viewport, layers) in state["objects"].items():
        try:
            _restore_transform(target, transforms)
            target.hide_render = hide_render
            if _writable(target, "hide_viewport"):
                target.hide_viewport = hide_viewport
            for layer, hidden in layers:
                target.hide_set(hidden, view_layer=layer)
        except (ReferenceError, RuntimeError, TypeError):
            LOG.exception("Could not restore production layout after rendering")
    for proxy, hide_render in state["proxies"].items():
        try:
            proxy.hide_render = hide_render
        except (ReferenceError, RuntimeError, TypeError):
            LOG.exception("Could not restore proxy visibility after rendering")
    for view_layer in scene.view_layers:
        view_layer.update()
    _restore_proxy_viewports(scene)
    VIEWPORT_RESTORE_SCENES[scene.as_pointer()] = scene
    if not bpy.app.timers.is_registered(_finish_viewport_restore):
        bpy.app.timers.register(_finish_viewport_restore, first_interval=0.1)


@persistent
def _render_load_pre(unused):
    for state in list(RENDER_STATES.values()):
        _render_restore(state["scene"])
    RENDER_STATES.clear()
    VIEWPORT_RESTORE_SCENES.clear()
    if bpy.app.timers.is_registered(_finish_viewport_restore):
        bpy.app.timers.unregister(_finish_viewport_restore)


@persistent
def _render_load_post(unused):
    for scene in bpy.data.scenes:
        if any(obj.get("is_proxy") for obj in scene.objects):
            _arm_render_safety(scene)


def _deferred_render_setup():
    if getattr(bpy.data, "scenes", None) is None:
        return 0.1
    _render_load_post(None)
    return None


RENDER_HANDLERS = (
    (bpy.app.handlers.render_init, _render_prepare),
    (bpy.app.handlers.render_pre, _render_prepare),
    (bpy.app.handlers.render_complete, _render_restore),
    (bpy.app.handlers.render_cancel, _render_restore),
    (bpy.app.handlers.load_pre, _render_load_pre),
    (bpy.app.handlers.load_post, _render_load_post),
)


def _notice(operator, context, message, warning=False):
    operator.report({'WARNING'} if warning else {'INFO'}, message)
    if warning and context.window and not bpy.app.background:
        def draw(menu, unused_context):
            menu.layout.label(text=message, icon='ERROR')
        context.window_manager.popup_menu(draw, title="ProxyWrangler Warning")


def _remove_proxy(proxy):
    mesh = proxy.data
    bpy.data.objects.remove(proxy, do_unlink=True)
    if mesh and mesh.users == 0 and mesh.library is None:
        bpy.data.meshes.remove(mesh)


def _writable(obj, name):
    if obj.library is not None or not obj.is_editable:
        return False
    if name == "matrix_world" and obj.override_library:
        return not obj.override_library.is_system_override
    return not obj.is_property_readonly(name)


def _production_target(context, source):
    if source.users_scene != (context.scene,):
        raise RuntimeError("Asset is shared across scenes; use a separate placement per scene")
    if source.library is None and source.override_library and source.override_library.is_system_override:
        source.override_library.is_system_override = False
    if _writable(source, "matrix_world") and not (
        source.override_library and source.override_library.is_system_override
    ):
        return source
    if source.library is None:
        raise RuntimeError("Select the editable root of this library override hierarchy")
    collections = set()
    pending = [context.scene.collection]
    while pending:
        collection = pending.pop()
        if collection not in collections:
            collections.add(collection)
            pending.extend(collection.children)
    owners = [c for c in source.users_collection if c in collections and c.library is None and c.is_editable]
    if owners:
        target = source.override_create(remap_local_usages=False)
        if target is None:
            raise RuntimeError("Blender could not create a local library override")
        target.override_library.is_system_override = False
        linked = []
        unlinked = []
        try:
            for collection in owners:
                collection.objects.link(target)
                linked.append(collection)
                collection.objects.unlink(source)
                unlinked.append(collection)
            return target
        except Exception:
            for collection in unlinked:
                collection.objects.link(source)
            for collection in linked:
                collection.objects.unlink(target)
            bpy.data.objects.remove(target, do_unlink=True)
            raise
    pending = [context.scene.collection]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent.as_pointer() in visited:
            continue
        visited.add(parent.as_pointer())
        for collection in parent.children:
            if collection.library and source in collection.all_objects.values():
                hierarchy = collection.override_hierarchy_create(
                    context.scene, context.view_layer, do_fully_editable=True
                )
                if hierarchy:
                    for target in hierarchy.all_objects:
                        if target.override_library and target.override_library.reference == source:
                            if hierarchy.name not in parent.children:
                                parent.children.link(hierarchy)
                            if collection.name in parent.children:
                                parent.children.unlink(collection)
                            context.view_layer.update()
                            return target
                raise RuntimeError("Blender could not override this linked collection hierarchy")
            pending.append(collection)
    raise RuntimeError("Select the collection instance containing this linked asset")


def _snapshot(context, source):
    depsgraph = context.evaluated_depsgraph_get()
    world = source.evaluated_get(depsgraph).matrix_world.copy()
    if not _matrix_close(world, Matrix.LocRotScale(*world.decompose())):
        raise RuntimeError("Asset has a sheared world transform; use a placement without shear")
    if world.determinant() == 0.0:
        raise RuntimeError("Asset has a zero scale")
    inverse = world.inverted()
    parts = []
    if source.type == 'MESH':
        parts.append((source, Matrix.Identity(4)))
    for instance in depsgraph.object_instances:
        if instance.is_instance and instance.parent and instance.parent.original == source:
            if instance.object.type == 'MESH':
                parts.append((instance.object.original, inverse @ instance.matrix_world.copy()))
    if not parts:
        raise RuntimeError("Asset has no evaluated mesh geometry")
    merged = bmesh.new()
    result = None
    try:
        for original, transform in parts:
            evaluated = original.evaluated_get(depsgraph)
            mesh = bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)
            try:
                mesh.transform(transform)
                if transform.determinant() < 0:
                    mesh.flip_normals()
                merged.from_mesh(mesh)
            finally:
                bpy.data.meshes.remove(mesh)
        if not merged.faces:
            raise RuntimeError("Asset has no polygon faces")
        result = bpy.data.meshes.new("PRXY_Geometry")
        merged.normal_update()
        merged.to_mesh(result)
        result.materials.clear()
        return result, world
    except Exception:
        if result and result.users == 0:
            bpy.data.meshes.remove(result)
        raise
    finally:
        merged.free()


def _freeze(context, proxy, ratio, cache=None):
    key = f"v1:{_mesh_digest(proxy.data)}:{ratio:.12g}"
    if cache is not None and key in cache:
        try:
            cached = cache[key]
            if cached.users:
                previous = proxy.data
                proxy.data = cached
                if previous.users == 0:
                    bpy.data.meshes.remove(previous)
                return
        except ReferenceError:
            pass
        cache.pop(key, None)
    modifier = proxy.modifiers.new("ProxyWrangler Collapse", 'DECIMATE')
    modifier.decimate_type = 'COLLAPSE'
    modifier.ratio = ratio
    context.view_layer.update()
    depsgraph = context.evaluated_depsgraph_get()
    frozen = bpy.data.meshes.new_from_object(proxy.evaluated_get(depsgraph), depsgraph=depsgraph)
    previous = proxy.data
    proxy.modifiers.clear()
    proxy.data = frozen
    frozen.materials.clear()
    for polygon in frozen.polygons:
        polygon.material_index = 0
    frozen["_pw_cache_key"] = key
    frozen["_pw_output_hash"] = _mesh_digest(frozen)
    if cache is not None:
        cache[key] = frozen
    if previous.users == 0:
        bpy.data.meshes.remove(previous)


def _target(proxy, scene):
    target = proxy.get("_pw_target")
    if isinstance(target, bpy.types.Object) and scene.objects.get(target.name) == target:
        if target == proxy or target.get("is_proxy"):
            raise RuntimeError("Production reference points to a proxy")
        return target
    if "_pw_target" not in proxy:
        name = proxy.get("production_mesh_ref", "")
        matches = [obj for obj in scene.objects if obj.name == name and obj.library is None]
        if len(matches) == 1 and matches[0] != proxy and not matches[0].get("is_proxy"):
            return matches[0]
    raise RuntimeError("Production target is missing from this scene; proxy retained")


class PROXYWRANGLER_OT_generate(bpy.types.Operator):
    bl_idname = "proxy_wrangler.generate"
    bl_label = "Generate Performance Proxies"
    bl_description = "Bake selected meshes and collection instances using the panel collapse ratio"
    bl_options = {'REGISTER', 'UNDO'}
    scope: EnumProperty(items=SCOPE_ITEMS, default='SELECTED')

    def execute(self, context):
        if bpy.app.is_job_running('RENDER') or RENDER_STATES:
            _notice(self, context, "Wait for rendering to finish before generating proxies", True)
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            _notice(self, context, "Switch to Object Mode before generating proxies", True)
            return {'CANCELLED'}
        selected = list(context.selected_objects if self.scope == 'SELECTED' else
                        (obj for obj in context.view_layer.objects if obj.visible_get(view_layer=context.view_layer)))
        sources = [o for o in selected if not o.get("is_proxy") and (
            o.type == 'MESH' or (o.instance_type == 'COLLECTION' and o.instance_collection)
        )]
        if not sources:
            _notice(self, context, "Select mesh objects or collection instances", True)
            return {'CANCELLED'}
        existing = {o.get("_pw_target") for o in context.scene.objects if o.get("is_proxy")
                    and isinstance(o.get("_pw_target"), bpy.types.Object)}
        sources = [obj for obj in sources if obj not in existing]
        if not sources:
            _notice(self, context, "No eligible assets without an existing proxy", True)
            return {'CANCELLED'}
        cache = _proxy_cache(context.scene) if context.scene.proxy_wrangler_share_geometry else None
        made = []
        errors = []
        for source in sources:
            proxy = None
            mesh = None
            target = None
            visibility = None
            try:
                original_name = source.name
                if source.library and context.scene.objects.get(source.name) != source:
                    source = next((obj for obj in context.scene.objects if obj.override_library
                                   and obj.override_library.reference == source), source)
                if source in existing:
                    raise RuntimeError("Asset already has a proxy")
                if source.users_scene != (context.scene,):
                    raise RuntimeError("Asset is shared across scenes; use a separate placement per scene")
                mesh, world = _snapshot(context, source)
                proxy = bpy.data.objects.new("PRXY_" + original_name, mesh)
                context.scene.collection.objects.link(proxy)
                proxy.matrix_world = world
                proxy.hide_render = True
                proxy.display_type = 'SOLID'
                proxy.show_wire = True
                proxy.show_all_edges = True
                ratio, triangles = _quality_ratio(context.scene, mesh)
                _freeze(context, proxy, ratio, cache)
                target = _production_target(context, source)
                visibility = (target.hide_viewport, target.hide_get(view_layer=context.view_layer))
                proxy["production_mesh_ref"] = original_name
                proxy["is_proxy"] = True
                proxy["_pw_collapse_ratio"] = ratio
                proxy["_pw_source_triangles"] = triangles
                proxy["_pw_target"] = target
                proxy["_pw_hide_viewport"] = visibility[0]
                proxy["_pw_hide_layer"] = visibility[1]
                proxy["_pw_view_layer"] = context.view_layer.name
                if _writable(target, "hide_viewport"):
                    target.hide_viewport = True
                else:
                    target.hide_set(True, view_layer=context.view_layer)
                existing.add(target)
                made.append(proxy)
            except Exception as exc:
                if target and visibility:
                    if _writable(target, "hide_viewport"):
                        target.hide_viewport = visibility[0]
                    target.hide_set(visibility[1], view_layer=context.view_layer)
                if proxy:
                    _remove_proxy(proxy)
                elif mesh and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
                errors.append(f"{source.name}: {exc}")
                LOG.exception("Proxy generation failed for %s", source.name)
        context.view_layer.update()
        if made:
            _arm_render_safety(context.scene)
            for obj in list(context.selected_objects):
                obj.select_set(False)
            for obj in made:
                obj.select_set(True)
            context.view_layer.objects.active = made[-1]
        message = f"Created {len(made)} proxies"
        if errors:
            message += f"; {len(errors)} skipped. {errors[0]}"
        _notice(self, context, message, bool(errors))
        return {'FINISHED'} if made else {'CANCELLED'}


class PROXYWRANGLER_OT_regenerate(bpy.types.Operator):
    bl_idname = "proxy_wrangler.regenerate"
    bl_label = "Regenerate Proxies"
    bl_description = "Rebuild proxy geometry at the current quality without changing layout placements"
    bl_options = {'REGISTER', 'UNDO'}
    scope: EnumProperty(items=SCOPE_ITEMS, default='SELECTED')

    def execute(self, context):
        if context.mode != 'OBJECT' or bpy.app.is_job_running('RENDER') or RENDER_STATES:
            _notice(self, context, "Use Object Mode and wait for rendering to finish", True)
            return {'CANCELLED'}
        proxies = _working_proxies(context, self.scope)
        if not proxies:
            _notice(self, context, "No matching proxies to regenerate", True)
            return {'CANCELLED'}
        cache = _proxy_cache(context.scene) if context.scene.proxy_wrangler_share_geometry else None
        count = 0
        errors = []
        counts = _proxy_target_counts(context.scene)
        for proxy in proxies:
            temporary = None
            mesh = None
            target = None
            visibility = None
            try:
                target = _validate_proxy(proxy, context.scene, counts)
                if proxy.get("_pw_view_layer", context.view_layer.name) != context.view_layer.name:
                    raise RuntimeError("Switch to the view layer used for proxy generation")
                if target.library is not None or not target.is_editable:
                    raise RuntimeError("Production placement must be local or a local override")
                visibility = (target.hide_viewport, target.hide_get(view_layer=context.view_layer))
                if _writable(target, "hide_viewport"):
                    target.hide_viewport = False
                target.hide_set(False, view_layer=context.view_layer)
                context.view_layer.update()
                mesh, world = _snapshot(context, target)
                temporary = bpy.data.objects.new("_PW_Rebuild", mesh)
                context.scene.collection.objects.link(temporary)
                temporary.hide_render = True
                temporary.matrix_world = world
                ratio, triangles = _quality_ratio(context.scene, mesh)
                _freeze(context, temporary, ratio, cache)
                previous = proxy.data
                proxy.data = temporary.data
                proxy["_pw_collapse_ratio"] = ratio
                proxy["_pw_source_triangles"] = triangles
                if previous.users == 0 and previous.library is None:
                    bpy.data.meshes.remove(previous)
                count += 1
            except Exception as exc:
                errors.append(f"{proxy.name}: {exc}")
                LOG.exception("Proxy regeneration failed for %s", proxy.name)
            finally:
                if temporary:
                    _remove_proxy(temporary)
                elif mesh and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
                if target and visibility:
                    if _writable(target, "hide_viewport"):
                        target.hide_viewport = visibility[0]
                    target.hide_set(visibility[1], view_layer=context.view_layer)
        context.view_layer.update()
        message = f"Regenerated {count} proxies"
        if errors:
            message += f"; {len(errors)} retained. {errors[0]}"
        _notice(self, context, message, bool(errors))
        return {'FINISHED'} if count else {'CANCELLED'}


class PROXYWRANGLER_OT_preflight(bpy.types.Operator):
    bl_idname = "proxy_wrangler.preflight"
    bl_label = "Render Preflight"
    bl_description = "Validate production references, libraries, render visibility and current-frame placement"

    def execute(self, context):
        errors = _preflight(context.scene)
        _notice(self, context, f"Preflight: {len(errors)} issues. {errors[0]}" if errors
                else "Preflight passed: ready to render", bool(errors))
        return {'CANCELLED'} if errors else {'FINISHED'}


class PROXYWRANGLER_OT_render(bpy.types.Operator):
    bl_idname = "proxy_wrangler.render"
    bl_label = "Preflight & Render"
    bl_description = "Block rendering on proxy preflight errors before starting Blender's renderer"
    animation: BoolProperty(default=False, options={'HIDDEN'})
    write_still: BoolProperty(default=False, options={'HIDDEN'})

    def execute(self, context):
        errors = _preflight(context.scene)
        if errors:
            _notice(self, context, f"Render blocked: {errors[0]}", True)
            return {'CANCELLED'}
        try:
            result = bpy.ops.render.render('EXEC_DEFAULT' if bpy.app.background else 'INVOKE_DEFAULT',
                                           animation=self.animation, write_still=self.write_still)
        except RuntimeError as exc:
            _render_restore(context.scene)
            _finish_viewport_restore()
            _notice(self, context, f"Render could not start: {exc}", True)
            return {'CANCELLED'}
        return {'CANCELLED'} if 'CANCELLED' in result else {'FINISHED'}


class PROXYWRANGLER_OT_restore(bpy.types.Operator):
    bl_idname = "proxy_wrangler.restore"
    bl_label = "Swap to Production Meshes"
    bl_description = "Transfer proxy world transforms to production objects and remove successful proxies"
    bl_options = {'REGISTER', 'UNDO'}
    scope: EnumProperty(items=SCOPE_ITEMS, default='ALL')

    def execute(self, context):
        if bpy.app.is_job_running('RENDER') or RENDER_STATES:
            _notice(self, context, "Wait for rendering to finish before restoring production assets", True)
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            _notice(self, context, "Switch to Object Mode before restoring production meshes", True)
            return {'CANCELLED'}
        context.view_layer.update()
        proxies = _working_proxies(context, self.scope)
        if not proxies:
            _notice(self, context, "No ProxyWrangler proxies in the active scene", True)
            return {'CANCELLED'}
        errors = []
        jobs = []
        counts = _proxy_target_counts(context.scene)
        for proxy in proxies:
            try:
                target = _validate_proxy(proxy, context.scene, counts)
                if proxy.get("_pw_view_layer", context.view_layer.name) != context.view_layer.name:
                    raise RuntimeError("Switch to the view layer used to generate this proxy")
                if not _writable(target, "matrix_world"):
                    raise RuntimeError("Production transform is read-only")
                depth = 0
                parent = target.parent
                while parent:
                    depth += 1
                    parent = parent.parent
                jobs.append((depth, proxy, target, proxy.matrix_world.copy()))
            except Exception as exc:
                errors.append(f"{proxy.name}: {exc}")
        count = 0
        for unused_depth, proxy, target, desired in sorted(jobs, key=lambda item: item[0]):
            old_transform = _capture_transform(target)
            old_viewport = target.hide_viewport
            old_layer = target.hide_get(view_layer=context.view_layer)
            try:
                target.matrix_world = desired
                if _writable(target, "hide_viewport"):
                    target.hide_viewport = False
                target.hide_set(False, view_layer=context.view_layer)
                context.view_layer.update()
                actual = target.evaluated_get(context.evaluated_depsgraph_get()).matrix_world
                if not _matrix_close(actual, desired):
                    raise RuntimeError("Constraints, animation or shear prevent exact placement; proxy retained")
                _remove_proxy(proxy)
                count += 1
            except Exception as exc:
                _restore_transform(target, old_transform)
                if _writable(target, "hide_viewport"):
                    target.hide_viewport = old_viewport
                target.hide_set(old_layer, view_layer=context.view_layer)
                context.view_layer.update()
                errors.append(f"{proxy.name}: {exc}")
                LOG.exception("Production restore failed for %s", target.name)
        message = f"Restored {count} production assets"
        if errors:
            message += f"; {len(errors)} retained. {errors[0]}"
        _notice(self, context, message, bool(errors))
        if not any(obj.get("is_proxy") for obj in context.scene.objects):
            _disarm_render_safety(context.scene)
        return {'FINISHED'} if count else {'CANCELLED'}


class VIEW3D_PT_proxy_wrangler(bpy.types.Panel):
    bl_label = "ProxyWrangler Engine"
    bl_idname = "VIEW3D_PT_proxy_wrangler"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "ProxyWrangler"

    def draw(self, context):
        layout = self.layout
        if context.mode != 'OBJECT':
            row = layout.row()
            row.alert = True
            row.label(text="Object Mode required", icon='ERROR')
        column = layout.column(align=True)
        column.enabled = context.mode == 'OBJECT'
        column.prop(context.scene, "proxy_wrangler_quality")
        row = column.row()
        row.enabled = context.scene.proxy_wrangler_quality in {'CUSTOM', 'ADAPTIVE'}
        row.prop(context.scene, "proxy_wrangler_collapse_percentage", slider=True)
        if context.scene.proxy_wrangler_quality != 'CUSTOM':
            column.prop(context.scene, "proxy_wrangler_min_triangles")
        column.prop(context.scene, "proxy_wrangler_share_geometry")
        column.label(text="Applies on generate / regenerate")
        for label, operator, icon in (
            ("Generate", "proxy_wrangler.generate", 'MOD_DECIM'),
            ("Regenerate", "proxy_wrangler.regenerate", 'FILE_REFRESH'),
            ("Restore Production", "proxy_wrangler.restore", 'SHADING_SOLID'),
        ):
            column.label(text=label)
            row = column.row(align=True)
            row.operator(operator, text="Selected", icon=icon).scope = 'SELECTED'
            row.operator(operator, text="All").scope = 'ALL'
        proxies = [obj for obj in context.scene.objects if obj.get("is_proxy") and obj.type == 'MESH']
        column.label(text=f"{len(proxies)} proxies / {len({obj.data for obj in proxies})} unique meshes")
        column.separator()
        column.operator("proxy_wrangler.preflight", icon='CHECKMARK')
        column.operator("proxy_wrangler.render", text="Preflight & Render · F12", icon='RENDER_STILL')
        column.operator("proxy_wrangler.render", text="Preflight & Animation", icon='RENDER_ANIMATION').animation = True
        report = context.scene.get("_pw_preflight_report")
        if report:
            box = layout.box()
            box.label(text="Last preflight result")
            for line in report.splitlines()[:6]:
                box.label(text=line)
        layout.label(text="Linked assets use local overrides", icon='LIBRARY_DATA_OVERRIDE')
        layout.label(text="Automatic render visibility: On", icon='RENDER_STILL')
        if context.scene.get("_pw_render_warning"):
            box = layout.box()
            box.alert = True
            box.label(text="Render preparation needs attention", icon='ERROR')
            box.label(text=context.scene["_pw_render_warning"])


CLASSES = (
    PROXYWRANGLER_OT_generate, PROXYWRANGLER_OT_regenerate, PROXYWRANGLER_OT_restore,
    PROXYWRANGLER_OT_preflight, PROXYWRANGLER_OT_render, VIEW3D_PT_proxy_wrangler,
)


def _render_menu(menu, context):
    menu.layout.operator("proxy_wrangler.render", text="ProxyWrangler Preflight & Render", icon='RENDER_STILL')
    menu.layout.operator("proxy_wrangler.render", text="ProxyWrangler Preflight & Animation").animation = True
    menu.layout.separator()


def _register_keymaps():
    manager = bpy.context.window_manager
    keyconfig = manager.keyconfigs.addon if manager else None
    if not keyconfig:
        return
    keymap = keyconfig.keymaps.new(name="Screen", space_type='EMPTY')
    for animation in (False, True):
        item = keymap.keymap_items.new("proxy_wrangler.render", 'F12', 'PRESS', ctrl=animation)
        item.properties.animation = animation
        KEYMAPS.append((keymap, item))


def register():
    bpy.types.Object.production_mesh_ref = StringProperty(options={'HIDDEN'})
    bpy.types.Scene.proxy_wrangler_collapse_percentage = FloatProperty(
        name="Collapse Ratio",
        description="Percentage of geometry to retain in new proxies; lower values create lighter proxies",
        default=RATIO * 100.0,
        min=0.1,
        max=100.0,
        precision=1,
        subtype='PERCENTAGE',
    )
    bpy.types.Scene.proxy_wrangler_quality = EnumProperty(
        name="Quality",
        items=[('CUSTOM', "Custom Ratio", "Use the artist's collapse ratio without a triangle floor"),
               ('FAST', "Fast Layout", "Retain 1% with a minimum triangle floor"),
               ('BALANCED', "Balanced", "Retain 3% with a minimum triangle floor"),
               ('DETAILED', "Detailed Silhouette", "Retain 15% with a minimum triangle floor"),
               ('ADAPTIVE', "Adaptive", "Use the custom ratio with a minimum triangle floor")],
        default='CUSTOM',
    )
    bpy.types.Scene.proxy_wrangler_min_triangles = IntProperty(
        name="Minimum Triangles", default=64, min=4, max=100000,
        description="Raise the collapse ratio for small assets; the decimator's final count is approximate",
    )
    bpy.types.Scene.proxy_wrangler_share_geometry = BoolProperty(
        name="Share Matching Geometry", default=True,
        description="Reuse proxy meshes only when evaluated geometry and collapse settings match",
    )
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for handlers, callback in RENDER_HANDLERS:
        if callback not in handlers:
            handlers.append(callback)
    bpy.types.TOPBAR_MT_render.prepend(_render_menu)
    _register_keymaps()
    if not bpy.app.timers.is_registered(_deferred_render_setup):
        bpy.app.timers.register(_deferred_render_setup, first_interval=0.0)


def unregister():
    for keymap, item in KEYMAPS:
        try:
            keymap.keymap_items.remove(item)
        except (ReferenceError, RuntimeError):
            pass
    KEYMAPS.clear()
    bpy.types.TOPBAR_MT_render.remove(_render_menu)
    if bpy.app.timers.is_registered(_deferred_render_setup):
        bpy.app.timers.unregister(_deferred_render_setup)
    for handlers, callback in RENDER_HANDLERS:
        if callback in handlers:
            handlers.remove(callback)
    _render_load_pre(None)
    for scene in bpy.data.scenes:
        _disarm_render_safety(scene)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Object.production_mesh_ref
    del bpy.types.Scene.proxy_wrangler_collapse_percentage
    del bpy.types.Scene.proxy_wrangler_quality
    del bpy.types.Scene.proxy_wrangler_min_triangles
    del bpy.types.Scene.proxy_wrangler_share_geometry


if __name__ == "__main__":
    register()
