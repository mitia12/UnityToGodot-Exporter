extends SceneTree
# Verification helper for a Unity -> Godot asset migration.
#
#   godot --headless --path <project> --script res://verify_prefabs.gd -- res://<dest>/prefabs
#
# Loads every prefab scene and checks that it has a mesh, that EVERY mesh surface got a
# material override, that the object sits on the ground and that its size is plausible.
# Prints one line per prefab plus a summary; exit code 1 if anything is broken.

func _init() -> void:
	var args := OS.get_cmdline_user_args()
	var dir := args[0] if args.size() > 0 else "res://KitchenProps/prefabs"
	var verbose := args.size() > 1 and args[1] == "-v"
	var bad := 0
	var total := 0
	var surface_total := 0
	var surface_missing := 0
	var names := DirAccess.get_files_at(dir)
	names.sort()
	for f in names:
		if not f.ends_with(".tscn"):
			continue
		total += 1
		var ps: PackedScene = load(dir.path_join(f))
		if ps == null:
			print("FAIL load ", f)
			bad += 1
			continue
		var inst: Node = ps.instantiate()
		var mi: MeshInstance3D = null
		for c in inst.get_children():
			if c is MeshInstance3D:
				mi = c
		if mi == null or mi.mesh == null:
			print("%-40s !! NO MESH" % f)
			bad += 1
			inst.free()
			continue

		var surfs := mi.mesh.get_surface_count()
		var overrides := mi.get_surface_override_material_count()
		var assigned := 0
		var mat_names: PackedStringArray = []
		for i in surfs:
			var m = mi.get_surface_override_material(i)
			if m != null:
				assigned += 1
				if mat_names.size() < 6:
					mat_names.append(str(m.resource_name))
		surface_total += surfs
		surface_missing += surfs - assigned

		var aabb: AABB = mi.mesh.get_aabb()
		var top: Vector3 = mi.transform * aabb.end
		var bottom: Vector3 = mi.transform * aabb.position
		var ymin := minf(top.y, bottom.y)
		var ymax := maxf(top.y, bottom.y)
		var size := Vector3(aabb.size.x, absf(top.y - bottom.y), aabb.size.z)

		var flags := ""
		if assigned < surfs:
			flags += "  !! %d/%d surfaces without material" % [surfs - assigned, surfs]
			bad += 1
		if maxf(size.x, size.z) > 300.0 or ymax > 300.0:
			flags += "  !! suspicious size"
			bad += 1
		# informational only: props may legitimately hang on walls / sit on other objects
		if ymin < -0.35 or ymin > 0.35:
			flags += "  (not ground-aligned: y=%.2f)" % ymin

		if verbose or flags != "":
			print("%-40s surfs=%d mats=%s y=%.2f..%.2f size=(%.2f, %.2f, %.2f)%s" % [
				f, surfs, ",".join(mat_names), ymin, ymax, size.x, size.y, size.z, flags])
		inst.free()
	print("TOTAL=%d BAD=%d SURFACES=%d MISSING_MATERIALS=%d" % [total, bad, surface_total, surface_missing])
	quit(1 if bad > 0 else 0)

