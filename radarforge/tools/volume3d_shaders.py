"""GLSL for the 3-D view (tools/volume3d.py).

Programs:
  TRI_VS + BG_FS        the dark gradient behind the analysis styles
  TRI_VS + SKY_FS       realistic style: sky, sun and the ground (fields, section roads, wet ground, cloud shadows)
  TRI_VS + LIGHT_FS     one layer of the light volume: optical depth towards the light and straight up, for
                        every cell of a coarser copy of the grid (shadows and ambient occlusion, all styles)
  TRI_VS + RAY_FS       the ray marcher: Volume, Surfaces and Realistic
  SCENE_VS + SCENE_FS   floor, map lines and the box frame
  TRI_VS + PRESENT_FS   copies a reduced-resolution frame to the window
"""

TRI_VS = """
#version 330 core
out vec2 v_uv;
void main(){
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    v_uv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

BG_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform vec3 u_top;
uniform vec3 u_bottom;
void main(){
    float t = clamp(v_uv.y, 0.0, 1.0);
    frag = vec4(mix(u_bottom, u_top, t * t * (3.0 - 2.0 * t)), 1.0);
}
"""

PRESENT_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_img;
void main(){ frag = texture(u_img, v_uv); }
"""

SCENE_VS = """
#version 330 core
layout(location=0) in vec3 a_pos;
layout(location=1) in vec4 a_col;
layout(location=2) in vec2 a_uv;
uniform mat4 u_mvp;
uniform float u_zscale;
out vec4 v_col;
out vec2 v_uv;
void main(){
    v_col = a_col;
    v_uv = a_uv;
    gl_Position = u_mvp * vec4(a_pos.xy, a_pos.z * u_zscale, 1.0);
}
"""

SCENE_FS = """
#version 330 core
in vec4 v_col;
in vec2 v_uv;
out vec4 frag;
uniform sampler2D u_tex;
uniform int u_textured;
uniform float u_alpha;
void main(){
    if (u_textured == 1) {
        vec4 t = texture(u_tex, v_uv);
        frag = vec4(mix(v_col.rgb, t.rgb, t.a), 1.0);
    } else {
        frag = vec4(v_col.rgb, v_col.a * u_alpha);
    }
}
"""

# --------------------------------------------------------------------------- shared by the volume programs
COMMON = """
uniform vec3 u_bmin;           // the box in world units (z already exaggerated)
uniform vec3 u_bmax;
uniform vec3 u_size;           // grid size (nx, ny, nz)
uniform sampler3D u_vol;       // values scaled to 0..1 over the colour table's range (mipmapped)
uniform sampler3D u_noise;     // tileable noise (realistic cloud edges)
uniform sampler1D u_tf;        // volume style: colour and density for each value
uniform sampler3D u_lightv;    // optical depth to the light (r) and straight up (g)
uniform float u_zscale;
uniform int u_mode;            // 0 volume, 1 surfaces, 2 realistic
uniform int u_nlev;
uniform float u_lev[6];
uniform vec4 u_levcol[6];      // rgb, alpha
uniform float u_levside[6];    // +1: values above the level are inside the surface
uniform float u_density;       // volume style: opacity per world unit
uniform float u_sdens;         // surfaces style: shadow density per world unit
// realistic
uniform float u_lo;            // storage range of u_vol (dBZ)
uniform float u_hi;
uniform float u_base;          // cloud base, km
uniform float u_cloud_dbz;     // where cloud starts
uniform float u_rain_on;
uniform float u_blur_lod;      // mip level for the widened (cloud) field
uniform float u_sig_cloud;     // extinction per km inside cloud
uniform float u_sig_rain;      // ... in the heaviest rain

vec3 to_tex(vec3 p) {
    vec3 f = (p - u_bmin) / (u_bmax - u_bmin);
    return (f * (u_size - 1.0) + 0.5) / u_size;
}
float val(vec3 p) { return textureLod(u_vol, to_tex(p), 0.0).r; }
vec2 lightv(vec3 p) { return textureLod(u_lightv, clamp((p - u_bmin) / (u_bmax - u_bmin), 0.0, 1.0), 0.0).rg; }

// realistic: extinction per km of cloud (x) and rain (y) at world point p
vec2 real_density(vec3 p) {
    vec3 tp = to_tex(p);
    float dbz = mix(u_lo, u_hi, textureLod(u_vol, tp, 0.0).r);
    float wide = mix(u_lo, u_hi, textureLod(u_vol, tp, u_blur_lod).r);
    vec3 k = vec3(p.xy, p.z / u_zscale);                       // km
    float nb = texture(u_noise, k / 7.3).r;
    float ns = texture(u_noise, k / 1.9 + vec3(0.37, 0.11, 0.73)).r;
    float hb = u_base + (nb - 0.5) * 0.35;
    // cloud reaches a little beyond the echo (droplets too small for the radar); billowed edges from the noise
    // (weak clear-air echo - insects, dust - stays clear: the noise only shapes edges near the threshold)
    float field = max(dbz, wide + 4.0);
    float nf = texture(u_noise, k / 0.7 + vec3(0.71, 0.29, 0.53)).r;
    float c = (field - u_cloud_dbz) / 10.0 + (0.62 * nb + 0.38 * ns - 0.5) * 0.85 + (nf - 0.5) * 0.25;
    float cloud = smoothstep(0.0, 0.28, c) * smoothstep(hb - 0.06, hb + 0.12, k.z);
    float rain = 0.0;
    if (u_rain_on > 0.5 && k.z < hb + 0.5) {
        float streak = texture(u_noise, vec3(k.xy / 0.8, k.z / 10.0)).r;
        rain = smoothstep(20.0, 58.0, dbz) * (0.25 + 0.75 * streak) * (1.0 - smoothstep(hb - 0.25, hb + 0.45, k.z));
    }
    // fade out towards the sides of the box (no cliff where the box cuts a storm off)
    vec2 e = min(p.xy - u_bmin.xy, u_bmax.xy - p.xy);
    float edge = smoothstep(0.0, 2.5, min(e.x, e.y) + (nb - 0.5) * 2.0);
    return vec2(cloud * u_sig_cloud, rain * u_sig_rain) * edge;
}

// extinction for the light volume: per world unit (volume, surfaces) or per km (realistic)
float occ(vec3 p) {
    if (u_mode == 2) { vec2 d = real_density(p); return d.x + d.y; }
    float v = val(p);
    if (u_mode == 0) return texture(u_tf, v).a * u_density;
    float d = 0.0;
    for (int k = 0; k < 6; k++) {
        if (k >= u_nlev) break;
        d += smoothstep(-0.006, 0.006, u_levside[k] * (v - u_lev[k])) * u_levcol[k].a;
    }
    return d * u_sdens;
}
"""

SKYFN = """
uniform vec3 u_tauR;
uniform float u_tauM;
uniform float u_E0;
float airmass(float s) {
    float el = max(degrees(asin(clamp(s, -1.0, 1.0))), 0.0);
    return 1.0 / (sin(radians(el)) + 0.50572 * pow(el + 6.07995, -1.6364));
}
vec3 sky(vec3 rd, vec3 sd) {
    vec3 tt = u_tauR + u_tauM;
    float mv = airmass(max(rd.z, 0.0));
    float ms = airmass(max(sd.z, -0.1));
    float mu = dot(rd, sd);
    float pr = 0.0596831 * (1.0 + mu * mu);
    float g = 0.76;
    float pm = (1.0 - g * g) / (12.566371 * pow(1.0 + g * g - 2.0 * g * mu, 1.5));
    vec3 scat = (u_tauR * pr + u_tauM * pm) / tt;
    float x = clamp((sd.z + 0.10) / 0.12, 0.0, 1.0);
    float day = x * x * (3.0 - 2.0 * x);
    // (sunlight reaches the scattering air high up, through less of the atmosphere than at the ground)
    return u_E0 * scat * (1.0 - exp(-tt * mv)) * exp(-(u_tauR * 0.22 + u_tauM * 0.5) * ms) * day
         + vec3(0.0006, 0.0009, 0.0020);
}
// haze that thins with height (1.6 km scale height): the fraction of light lost between heights h0 and h1 (km)
// over a path d km long, for a ground visibility of vis km
float haze(float h0, float h1, float d, float vis) {
    const float H = 1.6;
    float dh = h0 - h1;
    float f = abs(dh) < 0.02 ? exp(-max(h1, 0.0) / H)
                             : H * (exp(-max(h1, 0.0) / H) - exp(-max(h0, 0.0) / H)) / dh;
    return 1.0 - exp(-1.6 * d / vis * f);
}
vec3 aces(vec3 x) { return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0); }
vec3 to_screen(vec3 c, float exposure) { return pow(aces(c * exposure), vec3(1.0 / 2.2)); }
"""

LIGHT_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
""" + COMMON + """
uniform float u_layer_f;       // this layer's height in the box, 0..1
uniform vec3 u_ldir;           // towards the light, world units
uniform float u_lstep;

float march(vec3 p, vec3 d, float ratio) {
    vec3 ds = mix(d, vec3(1e-6), lessThan(abs(d), vec3(1e-6)));
    vec3 inv = 1.0 / ds;
    vec3 tm = max((u_bmin - p) * inv, (u_bmax - p) * inv);
    float t1 = min(min(tm.x, tm.y), tm.z);
    if (t1 <= 0.0) return 0.0;
    int n = int(clamp(ceil(t1 / u_lstep), 1.0, 128.0));
    float dt = t1 / float(n);
    float s = 0.0;
    for (int i = 0; i < 128; i++) {
        if (i >= n) break;
        s += occ(p + d * ((float(i) + 0.5) * dt));
    }
    return s * dt * ratio;
}

void main() {
    vec3 p = mix(u_bmin, u_bmax, vec3(v_uv, u_layer_f));
    float rs = u_mode == 2 ? length(vec3(u_ldir.xy, u_ldir.z / u_zscale)) : 1.0;
    float ru = u_mode == 2 ? 1.0 / u_zscale : 1.0;
    frag = vec4(march(p, u_ldir, rs), march(p, vec3(0.0, 0.0, 1.0), ru), 0.0, 1.0);
}
"""

SKY_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
""" + COMMON + SKYFN + """
uniform mat4 u_inv;
uniform vec3 u_sunp;           // towards the sun (real proportions): the sky
uniform vec3 u_lightp;         // towards what lights the storm (the sun, or the moon at night)
uniform vec3 u_lightw;         // ... in the exaggerated world
uniform float u_ground_sh;     // how dark cloud shadows on the ground are
uniform vec3 u_sun_col;
uniform vec3 u_sky_amb;
uniform float u_vis;
uniform float u_exposure;
uniform int u_has_grid;
uniform vec4 u_flash;          // world position, strength
uniform float u_flash_sky;

float hash(vec2 c) { return fract(sin(dot(c, vec2(127.1, 311.7))) * 43758.5453); }

vec3 ground(vec3 P, float dist) {
    vec2 g = P.xy;
    const float sect = 1.609;                                   // section-line fields and roads (1 mile)
    vec2 cell = floor(g / sect);
    float h1 = hash(cell), h2 = hash(cell + 31.7);
    vec3 alb = mix(vec3(0.075, 0.105, 0.040), vec3(0.165, 0.145, 0.078), h1);
    if (h2 > 0.78) alb = vec3(0.125, 0.098, 0.066);             // ploughed
    alb *= 0.8 + 0.4 * texture(u_noise, vec3(g / 2.7, 0.37)).r;
    float far = smoothstep(7.0, 38.0, dist);                    // (no shimmer far away: the average colour)
    alb = mix(alb, vec3(0.112, 0.118, 0.062), far);
    vec2 fr = abs(fract(g / sect + 0.5) - 0.5) * sect;
    float road = (1.0 - smoothstep(0.003, 0.009 + dist * 0.0009, min(fr.x, fr.y))) * (1.0 - far);
    alb = mix(alb, vec3(0.22, 0.20, 0.17), road * 0.45);
    float T = 1.0, amb = 1.0;
    if (u_has_grid == 1) {
        bool inside = all(greaterThanEqual(P.xy, u_bmin.xy)) && all(lessThanEqual(P.xy, u_bmax.xy));
        if (inside) {
            float dbz = mix(u_lo, u_hi, textureLod(u_vol, to_tex(vec3(P.xy, 0.15 * u_zscale)), 0.0).r);
            alb *= 1.0 - 0.38 * smoothstep(28.0, 52.0, dbz) * u_rain_on;          // wet ground
            amb = 0.25 + 0.75 * exp(-lightv(vec3(P.xy, 0.0)).y * 0.3);              // the storm hides the sky
        }
        // the cloud's shadow: where the path to the sun enters the box, the light volume has the rest
        vec3 d = mix(u_lightw, vec3(1e-6), lessThan(abs(u_lightw), vec3(1e-6)));
        vec3 inv = 1.0 / d;
        vec3 ta = (u_bmin - P) * inv, tb = (u_bmax - P) * inv;
        vec3 tn = min(ta, tb), tf = max(ta, tb);
        float t0 = max(max(tn.x, tn.y), max(tn.z, 0.0));
        float t1 = min(min(tf.x, tf.y), tf.z);
        if (t1 > t0 && u_lightw.z > 0.0) T = mix(1.0, exp(-lightv(P + u_lightw * (t0 + 1e-4)).x), u_ground_sh);
    }
    vec3 E = u_sun_col * max(u_lightp.z, 0.0) * T * 1.1 + u_sky_amb * amb * 0.9;
    if (u_flash.w > 0.0) {
        float r = length(vec3(P.xy - u_flash.xy, u_flash.z / u_zscale));
        E += vec3(0.75, 0.8, 1.0) * u_flash.w * 0.8 / (1.0 + r * r * 0.08);
    }
    return alb * E;
}

void main() {
    vec2 ndc = v_uv * 2.0 - 1.0;
    vec4 pn = u_inv * vec4(ndc, -1.0, 1.0);
    vec4 pf = u_inv * vec4(ndc, 1.0, 1.0);
    vec3 ro = pn.xyz / pn.w;
    vec3 rd = normalize(pf.xyz / pf.w - ro);
    vec3 rdp = normalize(vec3(rd.xy, rd.z / u_zscale));
    vec3 col;
    if (rd.z < -1e-6 && ro.z > 0.0) {
        float t = -ro.z / rd.z;
        vec3 P = ro + rd * t;
        float dist = length(vec3(P.xy - ro.xy, ro.z / u_zscale));
        col = ground(P, dist);
        vec3 hz = sky(normalize(vec3(rdp.xy, 0.0) + vec3(0.0, 0.0, 0.03)), u_sunp);
        col = mix(col, hz, haze(ro.z / u_zscale, 0.0, dist, u_vis));
    } else {
        col = sky(rdp, u_sunp);
        float sd = dot(rdp, u_sunp);
        col += u_sun_col * 60.0 * smoothstep(0.99993, 0.99997, sd);
    }
    col += vec3(0.55, 0.6, 0.85) * u_flash_sky;
    frag = vec4(to_screen(col, u_exposure), 1.0);
}
"""

RAY_FS = """
#version 330 core
in vec2 v_uv;
out vec4 frag;
""" + COMMON + SKYFN + """
uniform mat4 u_inv;            // inverse of projection * view
uniform sampler3D u_ctx;       // reflectivity, for the outline around other products
uniform sampler1D u_pal;       // the colour table (opaque), for the cut face
uniform int u_ctx_on;
uniform float u_ctx_lev;
uniform vec4 u_ctx_col;
uniform int u_cut;
uniform vec4 u_plane;          // xyz normal, w distance: dot(n, p) > w is cut away
uniform float u_step;          // world units between samples
uniform int u_steps;
uniform vec3 u_light;          // towards the light (analysis styles)
uniform int u_shadow;
uniform int u_caps;            // close surfaces where the box walls cut them
uniform float u_cell;          // one grid cell, world units
uniform float u_rest;          // how much of the storm shows around slices / a tilt (1: all of it)
uniform int u_slice_v;         // a vertical slice ...
uniform vec4 u_vplane;         // ... on this plane (normal, distance)
uniform int u_slice_h;         // a horizontal slice ...
uniform float u_hz;            // ... at this height (world units)
uniform float u_slice_a;       // how opaque the slices are
uniform int u_tilt_on;         // one radar tilt drawn as the cone it really is
uniform float u_tilt_tan;      // tan(elevation)
uniform vec2 u_radar;          // the radar's position, box coordinates (km)
uniform int u_face_ref;        // slices show: 1 values from u_face_lo up (reflectivity), 0 anything but "none"
uniform float u_face_lo;
uniform float u_floorn;        // "no echo" (the product's floor value), 0..1
uniform int u_colorby;         // surfaces coloured by another product ...
uniform sampler3D u_col2;      // ... its grid (0..1 over its colour table)
uniform sampler1D u_pal2;      // ... and colour table
// realistic
uniform vec3 u_sunp;
uniform vec3 u_lightp;         // towards the light (sun or moon)
uniform float u_g;             // forward scattering (silver linings)
uniform float u_ms_a;          // how fast light fades going deeper into cloud (lower: softer, brighter insides)
uniform vec3 u_sun_col;
uniform vec3 u_sky_amb;
uniform float u_vis;
uniform float u_exposure;
uniform vec4 u_flash;

float cval(vec3 p) { return textureLod(u_ctx, to_tex(p), 0.0).r; }

vec3 grad(vec3 p, bool ctx) {
    vec3 ext = u_bmax - u_bmin;
    vec3 t = to_tex(p);
    vec3 d = 1.5 / u_size;                       // a wide stencil: smooth shading across grid cells
    float gx, gy, gz;
    if (ctx) {
        gx = textureLod(u_ctx, t + vec3(d.x, 0, 0), 0.0).r - textureLod(u_ctx, t - vec3(d.x, 0, 0), 0.0).r;
        gy = textureLod(u_ctx, t + vec3(0, d.y, 0), 0.0).r - textureLod(u_ctx, t - vec3(0, d.y, 0), 0.0).r;
        gz = textureLod(u_ctx, t + vec3(0, 0, d.z), 0.0).r - textureLod(u_ctx, t - vec3(0, 0, d.z), 0.0).r;
    } else {
        gx = textureLod(u_vol, t + vec3(d.x, 0, 0), 0.0).r - textureLod(u_vol, t - vec3(d.x, 0, 0), 0.0).r;
        gy = textureLod(u_vol, t + vec3(0, d.y, 0), 0.0).r - textureLod(u_vol, t - vec3(0, d.y, 0), 0.0).r;
        gz = textureLod(u_vol, t + vec3(0, 0, d.z), 0.0).r - textureLod(u_vol, t - vec3(0, 0, d.z), 0.0).r;
    }
    return vec3(gx / (2.0 * d.x * ext.x), gy / (2.0 * d.y * ext.y), gz / (2.0 * d.z * ext.z));
}

vec3 shade(vec3 base, vec3 n, vec3 rd, vec3 p) {
    vec3 v = -rd;
    float sh = 1.0, ao = 1.0;
    if (u_shadow == 1) {
        vec2 L = lightv(p + n * u_cell * 1.6);   // a little outside: the surface doesn't shadow itself
        sh = exp(-L.x);
        ao = 0.45 + 0.55 * exp(-L.y * 0.5);
    }
    float diff = max(dot(n, u_light), 0.0) * sh;
    vec3 fl = normalize(vec3(-u_light.xy, 0.25));            // a soft fill light from the other side
    float fill = max(dot(n, fl), 0.0);
    float skyl = 0.6 + 0.4 * n.z;
    vec3 hv = normalize(u_light + v);
    float spec = pow(max(dot(n, hv), 0.0), 32.0) * 0.32 * sh;
    return base * (0.48 * skyl * ao + 0.66 * diff + 0.22 * fill * ao) + vec3(spec);
}

void blend(inout vec4 acc, vec3 c, float a) {
    acc.rgb += (1.0 - acc.a) * a * c;
    acc.a += (1.0 - acc.a) * a;
}

// the second product's colour at p (surfaces coloured by it); a dim grey where it has nothing
vec3 color2(vec3 p, vec3 own) {
    vec4 c2 = texture(u_pal2, textureLod(u_col2, to_tex(p), 0.0).r);
    return c2.a > 0.04 ? c2.rgb : own * 0.25 + vec3(0.12);
}

// a flat face (slice, cut, tilt) showing the data: the colour table where there's echo, faint glass elsewhere
void face(inout vec4 acc, vec3 p, vec3 n, vec3 rd, float alpha) {
    float v = val(p);
    if (dot(n, rd) > 0.0) n = -n;
    float lit = 0.72 + 0.28 * max(dot(n, u_light), 0.0);
    bool has = u_face_ref == 1 ? v >= u_face_lo : abs(v - u_floorn) > 0.002;
    if (has) {
        blend(acc, texture(u_pal, v).rgb * lit, alpha);
    } else {
        blend(acc, vec3(0.55, 0.62, 0.75) * 0.35, 0.10 * alpha);
    }
}

float beam_h(vec2 xy) {                          // height of the tilt's beam centre over a point (km)
    float s = length(xy - u_radar);
    return s * u_tilt_tan + s * s / 16988.0;     // 4/3 earth: s^2 / (2 * 8494 km)
}

// a surface crossing between two samples: refine it, light it, add it
void surface(inout vec4 acc, vec3 pa, vec3 pb, float va, float vb, float L, vec4 col, float side, vec3 rd,
             bool ctx) {
    for (int it = 0; it < 3; it++) {
        float f = clamp((L - va) / (vb - va), 0.0, 1.0);
        vec3 pm = mix(pa, pb, f);
        float vm = ctx ? cval(pm) : val(pm);
        if ((va - L) * (vm - L) < 0.0) { pb = pm; vb = vm; } else { pa = pm; va = vm; }
    }
    vec3 ph = mix(pa, pb, clamp((L - va) / (vb - va), 0.0, 1.0));
    vec3 g = grad(ph, ctx);
    float glen = length(g);
    vec3 n = glen > 1e-6 ? -side * g / glen : -rd;
    if (dot(n, rd) > 0.0) n = -n;                 // seen from inside: light the side facing us
    float facing = abs(dot(n, rd));
    float a = col.a * (ctx ? 1.0 : u_rest);
    vec3 base = col.rgb;
    if (u_colorby == 1 && !ctx) base = color2(ph, col.rgb);
    vec3 c = shade(base, n, rd, ph);
    if (a < 0.999) {
        float rim = pow(1.0 - facing, 3.0);
        a = a + (1.0 - a) * rim * 0.65;                                  // glassy edges
        // glass in a dark colour (30 dBZ green) vanishes against the dark background: a lighter tint and a
        // bright rim keep its shape readable
        c = mix(c, c * 0.6 + vec3(0.35), 0.35) + mix(col.rgb, vec3(1.0), 0.55) * rim * 0.45;
    }
    blend(acc, c, a);
}

// where the ray enters the box already inside a surface (the box cuts the storm), close it with a flat cap
void caps(inout vec4 acc, vec3 p, float v, vec3 n, vec3 rd) {
    if (dot(n, rd) > 0.0) n = -n;
    for (int k = 0; k < 6; k++) {
        if (k >= u_nlev) break;
        if (u_levside[k] * (v - u_lev[k]) <= 0.0) continue;
        vec4 col = u_levcol[k];
        vec3 base = u_colorby == 1 ? color2(p, col.rgb) : col.rgb;
        vec3 c = shade(base, n, rd, p) * 0.92;
        float a = (col.a < 0.999 ? min(1.0, col.a * 1.25) : 1.0) * u_rest;
        if (col.a < 0.999) c = mix(c, c * 0.6 + vec3(0.35), 0.35);
        blend(acc, c, a);
    }
}

float hgp(float mu, float g) {               // Henyey-Greenstein, 1 for an even spread
    float g2 = g * g;
    return (1.0 - g2) / pow(1.0 + g2 - 2.0 * g * mu, 1.5);
}

// ------------------------------------------------------------------ realistic: clouds, rain shafts, light
void realistic(vec3 ro, vec3 rd, float t0, float t1, float jit) {
    vec3 rdp = normalize(vec3(rd.xy, rd.z / u_zscale));
    float rlen = length(vec3(rd.xy, rd.z / u_zscale));   // km per world unit along this ray
    float mu = dot(rdp, u_lightp);
    // multiple scattering: a few octaves, each dimmer, less forward and seeing less of the cloud (Wrenninge)
    float ph[4];
    float c = 1.0;
    for (int o = 0; o < 4; o++) {
        ph[o] = 0.72 * hgp(mu, u_g * c) + 0.28 * hgp(mu, -0.22 * c);
        c *= 0.5;
    }
    vec3 col = vec3(0.0);
    float T = 1.0;
    float dsum = 0.0, wsum = 0.0;
    float t = t0 + jit * u_step;
    for (int i = 0; i < 6000; i++) {
        if (i >= u_steps || t > t1) break;
        vec3 p = ro + rd * t;
        if (u_cut == 1 && dot(u_plane.xyz, p) > u_plane.w) { t += u_step; continue; }
        vec2 d = real_density(p);
        float dens = d.x + d.y;
        if (dens < 1e-4) {
            // empty air: stride while even the widened field is far from cloud
            float wide = mix(u_lo, u_hi, textureLod(u_vol, to_tex(p), u_blur_lod + 1.0).r);
            t += u_step * (wide + 14.0 < u_cloud_dbz ? 3.0 : 1.0);
            continue;
        }
        float dt = u_step * rlen;
        vec2 L = lightv(p);
        float rainf = d.y / dens;
        vec3 sun = vec3(0.0);
        float a = 1.0, b = 1.0;
        for (int o = 0; o < 4; o++) {
            sun += b * ph[o] * exp(-L.x * a);
            a *= u_ms_a; b *= 0.5;
        }
        sun *= u_sun_col;
        float k = p.z / u_zscale;
        vec3 amb = u_sky_amb * (0.15 + 0.85 * exp(-L.y * 0.22)) * 0.9
                 + u_sky_amb * 0.12 * exp(-k * 0.25);              // light from the ground below
        vec3 S = (sun + amb) * mix(vec3(0.97), vec3(0.42, 0.45, 0.50), rainf);
        if (u_flash.w > 0.0) {
            vec3 q = p - u_flash.xyz;
            float r = length(vec3(q.xy, q.z / u_zscale));
            S += vec3(0.78, 0.82, 1.0) * u_flash.w * (exp(-r / 2.2) * 2.5 + exp(-r / 9.0) * 0.6);
        }
        float Ts = exp(-dens * dt);
        float w = T * (1.0 - Ts);
        col += w * S;
        dsum += w * t * rlen;
        wsum += w;
        T *= Ts;
        if (T < 0.008) break;
        t += u_step;
    }
    float alpha = 1.0 - T;
    if (alpha < 0.002) discard;
    float dist = dsum / max(wsum, 1e-6);
    float hk = clamp(ro.z / u_zscale + rdp.z * dist, 0.0, 30.0);       // height of the cloud we see
    float h = haze(ro.z / u_zscale, hk, dist, u_vis);
    vec3 hz = sky(normalize(vec3(rdp.xy, 0.0) + vec3(0.0, 0.0, 0.03)), u_sunp);
    vec3 c3 = col / alpha * (1.0 - h) + hz * h;
    frag = vec4(to_screen(c3, u_exposure) * alpha, alpha);
}

void main() {
    vec2 ndc = v_uv * 2.0 - 1.0;
    vec4 pn = u_inv * vec4(ndc, -1.0, 1.0);
    vec4 pf = u_inv * vec4(ndc, 1.0, 1.0);
    vec3 ro = pn.xyz / pn.w;
    vec3 rd = normalize(pf.xyz / pf.w - ro);
    vec3 rds = mix(rd, vec3(1e-6), lessThan(abs(rd), vec3(1e-6)));
    vec3 inv = 1.0 / rds;
    vec3 t0s = (u_bmin - ro) * inv;
    vec3 t1s = (u_bmax - ro) * inv;
    vec3 tmin = min(t0s, t1s);
    vec3 tmax = max(t0s, t1s);
    float t0 = max(max(tmin.x, tmin.y), max(tmin.z, 0.0));
    float t1 = min(min(tmax.x, tmax.y), tmax.z);
    if (t1 <= t0) discard;
    // the face the ray comes in through (for caps): none when the camera is inside the box
    vec3 entry_n = vec3(0.0);
    if (t0 > 0.0) {
        if (tmin.x >= tmin.y && tmin.x >= tmin.z) entry_n = vec3(1.0, 0.0, 0.0);
        else if (tmin.y >= tmin.z) entry_n = vec3(0.0, 1.0, 0.0);
        else entry_n = vec3(0.0, 0.0, 1.0);
    }

    // a different start offset per pixel: no wood-grain banding
    float jit = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
    if (u_mode == 2) { realistic(ro, rd, t0, t1, jit); return; }
    float t = t0 + jit * u_step;
    vec4 acc = vec4(0.0);
    // where the ray crosses the slices (drawn when the march passes them, so they sit in the right order)
    float tsv = -1.0, tsh = -1.0;
    if (u_slice_v == 1) {
        float den = dot(u_vplane.xyz, rd);
        if (abs(den) > 1e-6) tsv = (u_vplane.w - dot(u_vplane.xyz, ro)) / den;
        if (tsv < t0 || tsv > t1) tsv = -1.0;
    }
    if (u_slice_h == 1 && abs(rd.z) > 1e-6) {
        tsh = (u_hz - ro.z) / rd.z;
        if (tsh < t0 || tsh > t1) tsh = -1.0;
    }
    float tprev = t;
    float fprev = u_tilt_on == 1 ? (ro + rd * t).z / u_zscale - beam_h((ro + rd * t).xy) : 0.0;
    vec3 pprev = ro + rd * t;
    float prev = val(pprev);
    float cprev = u_ctx_on == 1 ? cval(pprev) : 0.0;
    bool started = false;
    bool removed_before = false;
    for (int i = 0; i < 4096; i++) {
        if (i >= u_steps || t > t1) break;
        vec3 p = ro + rd * t;
        if (u_cut == 1) {
            if (dot(u_plane.xyz, p) > u_plane.w) {
                removed_before = true; tprev = t; t += u_step;
                if (u_tilt_on == 1) fprev = p.z / u_zscale - beam_h(p.xy);
                if (tsv >= 0.0 && tsv <= t) tsv = -1.0;               // (slices in the cut-away part go too)
                if (tsh >= 0.0 && tsh <= t) tsh = -1.0;
                continue;
            }
            if (removed_before) {
                // came in through the cut: show the data on the cut face
                removed_before = false;
                float den = dot(u_plane.xyz, rd);
                if (abs(den) > 1e-6) {
                    vec3 pc = ro + rd * ((u_plane.w - dot(u_plane.xyz, ro)) / den);
                    float vc = val(pc);
                    if (texture(u_tf, vc).a > 0.0005) {
                        vec3 n = -u_plane.xyz;
                        if (dot(n, rd) > 0.0) n = -n;
                        vec3 c = texture(u_pal, vc).rgb;
                        float sh = u_shadow == 1 ? 0.55 + 0.45 * exp(-lightv(pc + n * u_cell).x) : 1.0;
                        blend(acc, c * (0.62 + 0.38 * max(dot(n, u_light), 0.0) * sh), 1.0);
                        break;
                    }
                }
                started = false;
            }
        }
        // slices and the tilt cone the march just passed
        if (tsv >= 0.0 && tsv <= t && tsv > tprev - 1e-6) {
            face(acc, ro + rd * tsv, u_vplane.xyz, rd, u_slice_a);
            tsv = -1.0;
        }
        if (tsh >= 0.0 && tsh <= t && tsh > tprev - 1e-6) {
            face(acc, ro + rd * tsh, vec3(0.0, 0.0, 1.0), rd, u_slice_a);
            tsh = -1.0;
        }
        if (u_tilt_on == 1) {
            float fcur = p.z / u_zscale - beam_h(p.xy);
            if (fprev * fcur < 0.0 && started) {
                vec3 pc = mix(pprev, p, fprev / (fprev - fcur));
                // the cone's normal: up, tilted away from the radar by the beam's slope
                vec2 away = normalize(pc.xy - u_radar + vec2(1e-6));
                vec3 n = normalize(vec3(-away * (u_tilt_tan + length(pc.xy - u_radar) / 8494.0), 1.0 / u_zscale));
                face(acc, pc, n, rd, 0.94);
            }
            fprev = fcur;
        }
        if (acc.a > 0.985) break;
        tprev = t;
        float cur = val(p);
        float ccur = u_ctx_on == 1 ? cval(p) : 0.0;
        if (!started) {                          // first sample inside: nothing to compare with yet
            started = true;
            // came in through a side or the top of the box (not through the cut) already inside a surface
            if (u_mode == 1 && u_caps == 1 && i == 0 && dot(entry_n, entry_n) > 0.5 && entry_n.z < 0.5) {
                vec3 pe = ro + rd * t0;
                caps(acc, pe, val(pe), entry_n, rd);
                if (acc.a > 0.985) break;
            }
            prev = cur; cprev = ccur; pprev = p; t += u_step;
            continue;
        }
        if (u_mode == 0) {
            vec4 s = texture(u_tf, cur);
            if (s.a > 0.002) {
                float a = (1.0 - exp(-u_density * s.a * u_step)) * u_rest;
                vec3 c = s.rgb;
                vec3 g = grad(p, false);
                float glen = length(g);
                if (glen > 1e-5) {
                    vec3 n = -g / glen;
                    if (dot(n, rd) > 0.0) n = -n;
                    c = mix(c * 0.8, shade(c, n, rd, p), clamp(glen * 6.0, 0.0, 1.0));
                } else if (u_shadow == 1) {
                    vec2 L = lightv(p);
                    c *= 0.5 + 0.5 * exp(-L.x);
                }
                blend(acc, c, a);
            }
        } else {
            for (int k = 0; k < 6; k++) {
                if (k >= u_nlev) break;
                float L = u_lev[k];
                if ((prev - L) * (cur - L) < 0.0)
                    surface(acc, pprev, p, prev, cur, L, u_levcol[k], u_levside[k], rd, false);
            }
        }
        if (u_ctx_on == 1 && (cprev - u_ctx_lev) * (ccur - u_ctx_lev) < 0.0)
            surface(acc, pprev, p, cprev, ccur, u_ctx_lev, u_ctx_col, 1.0, rd, true);
        if (acc.a > 0.985) break;
        prev = cur; cprev = ccur; pprev = p;
        t += u_step;
    }
    if (acc.a <= 0.0) discard;
    frag = acc;                                  // premultiplied
}
"""
