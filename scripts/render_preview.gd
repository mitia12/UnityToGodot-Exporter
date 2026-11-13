extends SceneTree
# Renders a PNG preview of a scene, without opening the editor.
#
#   godot --path <project> --script res://render_preview.gd -- res://<scene>.tscn res://preview.png [width] [height]
#
# Requires a real display/GPU (do NOT pass --headless). Useful to eyeball a migration.

func _init() -> void:
	var args := OS.get_cmdline_user_args()
	var scene_path := args[0] if args.size() > 0 else ""
	var out_path := args[1] if args.size() > 1 else "res://preview.png"
	var w := int(args[2]) if args.size() > 2 else 1600
	var h := int(args[3]) if args.size() > 3 else 900

	DisplayServer.window_set_size(Vector2i(w, h))
	var ps: PackedScene = load(scene_path)
	if ps == null:
		push_error("cannot load " + scene_path)
		quit(1)
		return
	root.add_child(ps.instantiate())
	for i in 12:
		await process_frame
	var img: Image = root.get_texture().get_image()
	var err := img.save_png(out_path)
	print("saved ", out_path, " ", img.get_size(), " err=", err)
	quit()
