extends SceneTree
# Verification helper for a Unity -> Godot asset migration.
#
#   godot --headless --path <project> --script res://verify_prefabs.gd -- res://<dest>/Prefabs
#
# Loads every prefab scene, checks that it has a mesh, a material and a sane size,
# and prints one line per prefab plus a summary. Exit code 1 if anything is broken.

func _init() -> void:
	var args := OS.get_cmdline_user_args()
	var dir := args[0] if args.size() > 0 else "res://KitchenProps/Prefabs"
	var bad := 0
	var total := 0
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
		var line := "%-34s" % f
		if mi == null or mi.mesh == null:
			print(line, " !! NO MESH")
			bad += 1
		else:
			var aabb: AABB = mi.mesh.get_aabb()
			var top: Vector3 = mi.transform * aabb.end
			var bottom: Vector3 = mi.transform * aabb.position
			var mat = mi.get_surface_override_material(0)
			line += " surfs=%d" % mi.mesh.get_surface_count()
			line += " mat=%s" % (str(mat.resource_name) if mat else "!! NONE")
			line += " y=%.2f..%.2f" % [minf(top.y, bottom.y), maxf(top.y, bottom.y)]
			line += " size=(%.2f, %.2f, %.2f)" % [aabb.size.x, aabb.size.z, absf(top.y - bottom.y)]
			print(line)
			if mat == null:
				bad += 1
		inst.free()
	print("TOTAL=", total, " BAD=", bad)
	quit(1 if bad > 0 else 0)
