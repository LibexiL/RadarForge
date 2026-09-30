"""GLSL shaders (OpenGL 3.3 core)."""

RADAR_VS = """
#version 330 core
layout(location = 0) in vec2 a_pos;     // km, radar-centred AEQD
layout(location = 1) in float a_row;    // radial index
layout(location = 2) in vec2 a_bounds;  // az lo / hi (deg)
uniform vec4 u_view;                    // cx, cy, px_per_km, unused
uniform vec2 u_vp;                      // viewport size (px)
uniform vec2 u_offset;                  // radar position in view frame (km)
out vec2 v_pos;
flat out float v_row;
flat out vec2 v_bounds;
void main() {
    v_pos = a_pos;
    v_row = a_row;
    v_bounds = a_bounds;
    vec2 p = (a_pos + u_offset - u_view.xy) * u_view.z;
    gl_Position = vec4(p / (u_vp * 0.5), 0.0, 1.0);
}
"""

RADAR_FS = """
#version 330 core
in vec2 v_pos;
flat in float v_row;
flat in vec2 v_bounds;
uniform sampler2D u_data;
uniform sampler2D u_lut;
uniform float u_first;      // centre of first gate (km)
uniform float u_spacing;
uniform float u_elev;       // radians
uniform int u_ground;
uniform int u_ngates;
uniform int u_nrad;
uniform float u_dscale;
uniform float u_doffset;
uniform float u_lutmin;
uniform float u_lutmax;
uniform vec4 u_rf;
uniform int u_smooth;
uniform float u_alpha;
uniform float u_nyq;        // >0: raw (aliased) velocity -> unfold neighbours before blending
out vec4 frag;
const float AE = 8494.6667;

float slant(float s) {
    if (u_ground == 1) return s;
    float phi = s / AE;
    return AE * sin(phi) / cos(u_elev + phi);
}
bool nodata(float v) { return v < -59000.0; }
bool folded(float v) { return v > 59000.0; }
float unfold(float n, float c) {
    if (u_nyq <= 0.0) return n;
    float iv = 2.0 * u_nyq;
    return n - iv * floor((n - c) / iv + 0.5);
}

void main() {
    float s = length(v_pos);
    float r = slant(s);
    float gf = (r - (u_first - 0.5 * u_spacing)) / u_spacing;
    if (gf < 0.0 || gf >= float(u_ngates)) discard;
    int g = int(floor(gf));
    int row = int(v_row + 0.5);
    float v = texelFetch(u_data, ivec2(g, row), 0).r;
    if (nodata(v)) discard;
    if (folded(v)) { frag = vec4(u_rf.rgb, u_rf.a * u_alpha); return; }
    if (u_smooth == 1) {
        // bilinear blend with neighbouring gate and neighbouring radial
        float tg = gf - 0.5 - float(g);
        int g2 = tg < 0.0 ? g - 1 : g + 1;
        float wg = abs(tg);
        float az = degrees(atan(v_pos.x, v_pos.y));
        float span = mod(v_bounds.y - v_bounds.x + 360.0, 360.0);
        float fa = mod(az - v_bounds.x + 360.0, 360.0) / max(span, 1e-4) - 0.5;
        int r2 = fa < 0.0 ? row - 1 : row + 1;
        r2 = (r2 + u_nrad) % u_nrad;
        float wa = abs(fa);
        g2 = clamp(g2, 0, u_ngates - 1);
        float v10 = texelFetch(u_data, ivec2(g2, row), 0).r;
        float v01 = texelFetch(u_data, ivec2(g, r2), 0).r;
        float v11 = texelFetch(u_data, ivec2(g2, r2), 0).r;
        float w00 = (1.0 - wg) * (1.0 - wa);
        float w10 = wg * (1.0 - wa);
        float w01 = (1.0 - wg) * wa;
        float w11 = wg * wa;
        float acc = v * w00; float wsum = w00;
        if (!nodata(v10) && !folded(v10)) { acc += unfold(v10, v) * w10; wsum += w10; }
        if (!nodata(v01) && !folded(v01)) { acc += unfold(v01, v) * w01; wsum += w01; }
        if (!nodata(v11) && !folded(v11)) { acc += unfold(v11, v) * w11; wsum += w11; }
        v = acc / wsum;
    }
    float d = v * u_dscale + u_doffset;
    float t = (d - u_lutmin) / (u_lutmax - u_lutmin);
    if (t < 0.0) discard;
    vec4 c = texture(u_lut, vec2(clamp(t, 0.0, 0.9999), 0.5));
    if (c.a < 0.01) discard;
    frag = vec4(c.rgb, c.a * u_alpha);
}
"""

LINE_VS = """
#version 330 core
layout(location = 0) in vec2 a_pos;
uniform vec4 u_view;
uniform vec2 u_vp;
void main() {
    vec2 p = (a_pos - u_view.xy) * u_view.z;
    gl_Position = vec4(p / (u_vp * 0.5), 0.0, 1.0);
}
"""

LINE_GS = """
#version 330 core
layout(lines) in;
layout(triangle_strip, max_vertices = 4) out;
uniform vec2 u_vp;
uniform float u_width;
void main() {
    vec2 a = gl_in[0].gl_Position.xy;
    vec2 b = gl_in[1].gl_Position.xy;
    vec2 d = (b - a) * u_vp;
    float len = length(d);
    if (len < 1e-6) d = vec2(1.0, 0.0); else d /= len;
    vec2 n = vec2(-d.y, d.x) * (u_width * 0.5) / (u_vp * 0.5);
    gl_Position = vec4(a + n, 0.0, 1.0); EmitVertex();
    gl_Position = vec4(a - n, 0.0, 1.0); EmitVertex();
    gl_Position = vec4(b + n, 0.0, 1.0); EmitVertex();
    gl_Position = vec4(b - n, 0.0, 1.0); EmitVertex();
    EndPrimitive();
}
"""

LINE_FS = """
#version 330 core
uniform vec4 u_color;
out vec4 frag;
void main() { frag = u_color; }
"""

# full-window copy of the cached scene (hover / cursor-only repaints)
BLIT_VS = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 t = vec2(float(gl_VertexID & 1), float(gl_VertexID >> 1));
    v_uv = t;
    gl_Position = vec4(t * 2.0 - 1.0, 0.0, 1.0);
}
"""

BLIT_FS = """
#version 330 core
in vec2 v_uv;
uniform sampler2D u_tex;
out vec4 frag;
void main() { frag = texture(u_tex, v_uv); }
"""
