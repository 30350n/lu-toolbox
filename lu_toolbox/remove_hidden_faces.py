import bpy, bmesh
from mathutils import Vector, Matrix
from bpy.props import IntProperty, FloatProperty, BoolProperty
import math
import numpy as np

LUTB_HSR_ID = "LUTB_HSR"

class LUTB_OT_remove_hidden_faces(bpy.types.Operator):
    """Remove hidden interior geometry from the model."""
    bl_idname = "lutb.remove_hidden_faces"
    bl_label = "Remove Hidden Faces"

    autoremove           : BoolProperty(default=True, description=""\
        "Automatically remove hidden polygons. "\
        "Disabling this results in hidden polygons being assigned to the objects Face Maps"
    )
    vc_pre_pass          : BoolProperty(default=True, description=""\
        "Use vertex color baking based pre-pass to quickly sort out faces that are"\
        "definitely visible.")
    vc_pre_pass_samples  : IntProperty(min=1, default=32, description=""\
        "Number of samples to render for vertex color pre-pass")
    tris_to_quads        : BoolProperty(default=True, description=""\
        "Convert models triangles to quads for faster, more efficient HSR. "\
        "Quads are then converted back to tris afterwards. "\
        "Disabling this may result in slower HSR processing")
    pixels_between_verts : IntProperty(min=0, default=5, description="")
    samples              : IntProperty(min=1, default=8, description=""\
        "Number of samples to render for HSR")
    threshold            : FloatProperty(min=0, default=0.01, max=1)
    use_ground_plane     : BoolProperty(default=False, description=""\
        "Add a ground plane that contributes occlusion to the model during HSR so that "\
        "the underside of the model gets removed. Before enabling this option, make "\
        "sure your model does not extend below the default ground plane in LDD")

    @classmethod
    def poll(cls, context):
        return (
            context.object
            and context.object.type == "MESH"
            and context.mode == "OBJECT"
            and context.scene.render.engine == "CYCLES"
        )

    def execute(self, context):
        scene = context.scene
        target_obj = context.object
        mesh = target_obj.data

        loop_counts = np.empty(len(mesh.polygons), dtype=int)
        mesh.polygons.foreach_get("loop_total", loop_counts)
        if loop_counts.max() > 4:
            self.report({"ERROR"}, "Mesh needs to consist of tris or quads only!")
            return {"CANCELLED"}

        ground_plane = None
        if self.use_ground_plane:
            ground_plane = self.add_ground_plane()

        hidden_objects = []
        for obj in list(scene.collection.all_objects):
            if obj not in {target_obj, ground_plane} and not obj.hide_render:
                obj.hide_render = True
                hidden_objects.append(obj)

        scene_override = self.setup_scene_override(context)

        if self.vc_pre_pass:
            visible = self.compute_vc_pre_pass(context, scene_override)
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="DESELECT")
            bpy.ops.object.mode_set(mode="OBJECT")
            mesh.polygons.foreach_set("select", visible)
        else:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.object.mode_set(mode="OBJECT")

        if self.tris_to_quads:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.tris_convert_to_quads()
            bpy.ops.object.mode_set(mode="OBJECT")

        select = np.empty(len(mesh.polygons), dtype=bool)
        mesh.polygons.foreach_get("select", select)
        indices = np.where(select)[0]
        faces = [mesh.polygons[index] for index in indices]

        image = self.bake_to_image(context, scene_override, faces)
        hidden_indices = self.get_hidden_from_image(image, faces)

        bpy.ops.object.mode_set(mode="EDIT")
        context.tool_settings.mesh_select_mode = (False, False, True)
        bpy.ops.mesh.select_all(action="DESELECT")
        bpy.ops.object.mode_set(mode="OBJECT")

        select = np.zeros(len(mesh.polygons), dtype=bool)
        select[hidden_indices] = True
        mesh.polygons.foreach_set("select", select)

        if self.autoremove:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.delete(type="FACE")
            bpy.ops.object.mode_set(mode="OBJECT")

        bpy.data.scenes.remove(scene_override)

        for obj in hidden_objects:
            obj.hide_render = False

        if ground_plane:
            bpy.data.objects.remove(ground_plane)

        return {"FINISHED"}

    def add_ground_plane(self):
        bm = bmesh.new()
        matrix = Matrix.Diagonal((1000, 1000, 100, 1)) @ Matrix.Translation((0, 0, -0.5))
        bmesh.ops.create_cube(bm, size=1.0, matrix=matrix)
        mesh = bpy.data.meshes.new(LUTB_HSR_ID)
        obj = bpy.data.objects.new(LUTB_HSR_ID, mesh)
        bm.to_mesh(mesh)

        if not (material := bpy.data.materials.get(LUTB_HSR_ID + "_GP")):
            material = bpy.data.materials.new(LUTB_HSR_ID + "_GP")
            material.use_nodes = True
            nodes = material.node_tree.nodes
            nodes.clear()
            node_diffuse = nodes.new("ShaderNodeBsdfDiffuse")
            node_diffuse.inputs["Color"].default_value = (0, 0, 0, 1)
            node_output = nodes.new("ShaderNodeOutputMaterial")
            material.node_tree.links.new(node_diffuse.outputs[0], node_output.inputs[0])

        mesh.materials.append(material)

        return obj

    def compute_vc_pre_pass(self, context, scene_override):
        cycles = scene_override.cycles
        cycles.samples = self.vc_pre_pass_samples
        cycles.max_bounces = 12
        cycles.diffuse_bounces = 12
        scene_override.render.bake.target = "VERTEX_COLORS"

        return len(context.object.data.polygons) * [True]

    def setup_uv_layer(self, context, mesh, faces, size, size_pixels):
        uv_layer = mesh.uv_layers.new(name=LUTB_HSR_ID)
        uv_layer.active = True

        pbv_p_1 = self.pixels_between_verts + 1
        offsets = np.array((
            np.array((0, 0)) + np.array((-0.01, 0.00)) * pbv_p_1,
            np.array((1, 0)) + np.array(( 1.00, 0.00)) * pbv_p_1,
            np.array((1, 1)) + np.array(( 1.00, 1.01)) * pbv_p_1,
            np.array((0, 1)) + np.array((-0.01, 1.01)) * pbv_p_1,
        )) / size_pixels

        size_inv = 1 / size
        uv_data = np.zeros((len(mesh.loops), 2))
        for i, face in enumerate(faces):
            target = np.array((i % size, i // size)) * size_inv
            uv_data[face.loop_indices] = target + offsets[:face.loop_total]
        uv_layer.data.foreach_set("uv", uv_data.flatten())

        return uv_layer

    def setup_scene_override(self, context):
        scene_override = context.scene.copy()

        scene_override.world = get_overexposed_world()
        cycles = scene_override.cycles
        cycles.use_denoising = False
        cycles.use_fast_gi = False
        cycles.sample_clamp_direct = 0.0
        cycles.sample_clamp_indirect = 0.0

        bake_settings = scene_override.render.bake
        bake_settings.use_pass_direct = True
        bake_settings.use_pass_indirect = True
        bake_settings.use_pass_diffuse = True

        return scene_override

    def bake_to_image(self, context, scene, faces):
        obj = context.object
        mesh = context.object.data

        size = math.ceil(math.sqrt(len(faces)))
        quadrant_size = 2 + self.pixels_between_verts
        size_pixels = size * quadrant_size

        uv_layer = self.setup_uv_layer(context, mesh, faces, size, size_pixels)

        image = bpy.data.images.get(LUTB_HSR_ID)
        if image and tuple(image.size) != (size_pixels, size_pixels):
            bpy.data.images.remove(image)
            image = None
        if not image:
            image = bpy.data.images.new(LUTB_HSR_ID, size_pixels, size_pixels)

        original_materials = []
        for i, material_slot in enumerate(obj.material_slots):
            original_materials.append(material_slot.material)
            obj.material_slots[i].material = get_overexposed_material(image)

        cycles = scene.cycles
        cycles.samples = self.samples
        cycles.max_bounces = 8
        cycles.diffuse_bounces = 8
        scene.render.bake.target = "IMAGE_TEXTURES"

        context_override = context.copy()
        context_override["scene"] = scene
        bpy.ops.object.bake(context_override, type="DIFFUSE", margin=0, use_clear=True)

        for i, material in enumerate(original_materials):
            obj.material_slots[i].material = material

        mesh.uv_layers.remove(uv_layer)

        return image

    def get_hidden_from_image(self, image, faces):
        size = math.ceil(math.sqrt(len(faces)))
        quadrant_size = 2 + self.pixels_between_verts
        size_pixels = size * quadrant_size
        size_sq = size ** 2
        size_pixels_sq = size_pixels ** 2

        pixels = np.empty(size_pixels_sq * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)

        sum_per_face = pixels.copy()
        sum_per_face = np.reshape(sum_per_face, (size_pixels_sq, 4))
        sum_per_face = np.delete(sum_per_face, 3, 1)
        sum_per_face = np.reshape(sum_per_face, (size_pixels_sq // quadrant_size, quadrant_size * 3))
        sum_per_face = np.sum(sum_per_face, axis=1)
        sum_per_face = np.reshape(sum_per_face, (size_pixels, size))
        sum_per_face = np.swapaxes(sum_per_face, 0, 1)
        sum_per_face = np.reshape(sum_per_face, (size_sq, quadrant_size))
        sum_per_face = np.sum(sum_per_face, axis=1)
        sum_per_face = np.reshape(sum_per_face, (size, size))
        sum_per_face = np.swapaxes(sum_per_face, 0, 1)
        sum_per_face = np.reshape(sum_per_face, (1, size_sq))[0][:len(faces)]

        pixels_per_quad = quadrant_size ** 2
        pixels_per_tri  = (pixels_per_quad + quadrant_size) / 2
        loops_per_face = np.array([face.loop_total for face in faces])
        pixels_per_face = np.array((pixels_per_tri, pixels_per_quad))[loops_per_face - 3]

        average_per_face = sum_per_face / pixels_per_face / 3

        indices = np.where(average_per_face < self.threshold)[0]

        return indices

def get_overexposed_material(image):
    name = "LUTB_overexposed"

    material = bpy.data.materials.get(name)
    if not material:
        material = bpy.data.materials.new(name)
        material.use_nodes = True
        nodes = material.node_tree.nodes

        node_texture = nodes.new("ShaderNodeTexImage")
        node_texture.name = "LUTB_TARGET"

    nodes = material.node_tree.nodes
    node_texture = nodes["LUTB_TARGET"]
    node_texture.image = image

    return material

def get_overexposed_world():
    name = "LUTB_overexposed"

    world = bpy.data.worlds.get(name)
    if not world:
        world = bpy.data.worlds.new(name)
        world.use_nodes = True
        nodes = world.node_tree.nodes

        node_background = nodes["Background"]
        node_background.inputs["Color"].default_value = (1, 1, 1, 1)
        node_background.inputs["Strength"].default_value = 100000

    return world

def register():
    bpy.utils.register_class(LUTB_OT_remove_hidden_faces)
    
def unregister():
    bpy.utils.unregister_class(LUTB_OT_remove_hidden_faces)
