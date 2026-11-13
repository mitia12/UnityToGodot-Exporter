extends SceneTree
# Cross-checks which normal-map convention a texture set uses, without opening the editor.
#
#   godot --headless --path . --script res://check_normal_convention.gd -- res://<dest>/textures
#
# Idea: for a height field h, the normal is N = (-h_u, -h_v, 1) / |...|, so h_u = -nx/nz and
# h_v = -ny/nz. The divergence (Laplacian) of that gradient is POSITIVE in concave areas —
# exactly where an AO map is dark. So correlating div(h) with (1 - AO) tells you how the
# green channel must be interpreted:
#
#   corr_GL positive  -> green points UP the image   (OpenGL / Y+)  -> leave the import alone
#   corr_DX positive  -> green points DOWN the image (DirectX / Y-) -> set
#                        process/normal_map_invert_y=true in the .import
#
# Pairs <name>_N.png with <name>_ORM.png (R = AO). The correlation is weak for stylised,
# hand-painted AO maps — trust the sign across the whole pack, and prefer the authoritative
# `bumpmap.flipGreenChannel` value in the Unity .meta when both are available.
#
# Note: the "Loaded resource as image file, this will not work on export" warnings are
# expected — the raw PNG has to be read without the import settings applied.

const S := 6          # central-difference radius in pixels
const STEP := 4       # sampling stride

func _init() -> void:
	var args := OS.get_cmdline_user_args()
	var dir := args[0] if args.size() > 0 else "res://KitchenProps/textures"

	var names := []
	for f in DirAccess.get_files_at(dir):
		if f.to_lower().ends_with("_n.png"):
			var base := f.substr(0, f.length() - 6)
			if FileAccess.file_exists(dir.path_join(base + "_ORM.png")):
				names.append(base)
	names.sort()

	print("%-22s %10s %10s   %s" % ["texture", "corr_GL", "corr_DX", "verdict"])
	var sum_gl := 0.0
	var sum_dx := 0.0
	var dx_votes := 0
	var gl_votes := 0
	for base in names:
		var n: Image = Image.load_from_file(dir.path_join(base + "_N.png"))
		var o: Image = Image.load_from_file(dir.path_join(base + "_ORM.png"))
		if n == null or o == null:
			continue
		n.convert(Image.FORMAT_RGBA8)
		o.convert(Image.FORMAT_RGBA8)
		var gl := _corr(o, n, true)
		var dx := _corr(o, n, false)
		sum_gl += gl
		sum_dx += dx
		var verdict := "ambiguous"
		if absf(gl - dx) > 0.004:
			verdict = "DirectX (Y-) -> invert_y" if dx > gl else "OpenGL (Y+)"
			if dx > gl:
				dx_votes += 1
			else:
				gl_votes += 1
		print("%-22s %10.4f %10.4f   %s" % [base, gl, dx, verdict])

	if names.is_empty():
		print("no <name>_N.png + <name>_ORM.png pairs found in ", dir)
		quit(1)
		return
	print("")
	print("mean corr_GL=%.4f  corr_DX=%.4f   votes: DirectX=%d  OpenGL=%d" % [
		sum_gl / names.size(), sum_dx / names.size(), dx_votes, gl_votes])
	if dx_votes > gl_votes:
		print("=> the PNGs use the DirectX convention: set process/normal_map_invert_y=true")
	elif gl_votes > dx_votes:
		print("=> the PNGs already use the OpenGL convention Godot expects: no flip needed")
	else:
		print("=> inconclusive, fall back to bumpmap.flipGreenChannel in the Unity .meta files")
	quit()

func _corr(o: Image, n: Image, plus_green: bool) -> float:
	var w := n.get_width()
	var h := n.get_height()
	var ao_v := PackedFloat32Array()
	var lap_v := PackedFloat32Array()
	for y in range(S + 1, h - S - 1, STEP):
		for x in range(S + 1, w - S - 1, STEP):
			var dhu := (_gu(n, x + S, y) - _gu(n, x - S, y)) / (2.0 * S)
			var dhv := (_gv(n, x, y + S, plus_green) - _gv(n, x, y - S, plus_green)) / (2.0 * S)
			ao_v.append(1.0 - o.get_pixel(x, y).r)
			lap_v.append(dhu + dhv)
	var cnt := float(ao_v.size())
	if cnt < 16.0:
		return 0.0
	var mx := 0.0
	var my := 0.0
	for i in ao_v.size():
		mx += ao_v[i]
		my += lap_v[i]
	mx /= cnt
	my /= cnt
	var sxy := 0.0
	var sxx := 0.0
	var syy := 0.0
	for i in ao_v.size():
		var dxv := ao_v[i] - mx
		var dyv := lap_v[i] - my
		sxy += dxv * dyv
		sxx += dxv * dxv
		syy += dyv * dyv
	return sxy / sqrt(maxf(1e-9, sxx * syy))

func _gu(n: Image, x: int, y: int) -> float:
	var c := n.get_pixel(x, y)
	var nx := c.r * 2.0 - 1.0
	var ny := c.g * 2.0 - 1.0
	var nz := sqrt(maxf(0.0001, 1.0 - nx * nx - ny * ny))
	return -nx / nz

func _gv(n: Image, x: int, y: int, plus_green: bool) -> float:
	var c := n.get_pixel(x, y)
	var nx := c.r * 2.0 - 1.0
	var ny := c.g * 2.0 - 1.0
	var nz := sqrt(maxf(0.0001, 1.0 - nx * nx - ny * ny))
	var v := ny / nz
	return v if plus_green else -v
