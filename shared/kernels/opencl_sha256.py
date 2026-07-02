OPENCL_KERNEL = r"""
__constant uint K[64] = {
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};

uint rotr(uint x, uint n) { return (x >> n) | (x << (32 - n)); }
uint Ch(uint x, uint y, uint z) { return (x & y) ^ (~x & z); }
uint Maj(uint x, uint y, uint z) { return (x & y) ^ (x & z) ^ (y & z); }
uint BSIG0(uint x) { return rotr(x, 2) ^ rotr(x, 13) ^ rotr(x, 22); }
uint BSIG1(uint x) { return rotr(x, 6) ^ rotr(x, 11) ^ rotr(x, 25); }
uint SSIG0(uint x) { return rotr(x, 7) ^ rotr(x, 18) ^ (x >> 3); }
uint SSIG1(uint x) { return rotr(x, 17) ^ rotr(x, 19) ^ (x >> 10); }
uint bswap32(uint x) { return ((x & 0xffU) << 24) | ((x & 0xff00U) << 8) | ((x >> 8) & 0xff00U) | ((x >> 24) & 0xffU); }

uint be32(__global const uchar *b, int off) {
    return ((uint)b[off] << 24) | ((uint)b[off + 1] << 16) | ((uint)b[off + 2] << 8) | (uint)b[off + 3];
}

void compress(uint w[64], uint st[8]) {
    for (int i = 16; i < 64; i++) w[i] = SSIG1(w[i - 2]) + w[i - 7] + SSIG0(w[i - 15]) + w[i - 16];
    uint a=st[0], b=st[1], c=st[2], d=st[3], e=st[4], f=st[5], g=st[6], h=st[7];
    for (int i = 0; i < 64; i++) {
        uint t1 = h + BSIG1(e) + Ch(e, f, g) + K[i] + w[i];
        uint t2 = BSIG0(a) + Maj(a, b, c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }
    st[0]+=a; st[1]+=b; st[2]+=c; st[3]+=d; st[4]+=e; st[5]+=f; st[6]+=g; st[7]+=h;
}

int hash_meets_target(uint st2[8], __global const uint *target) {
    // Bitcoin PoW vergleicht den Double-SHA256 als little-endian Integer.
    // target[] ist big-endian als 8x uint32. Daher werden die Hash-Wörter byte- und wortweise umgedreht.
    for (int i = 0; i < 8; i++) {
        uint hw = bswap32(st2[7 - i]);
        uint tw = target[i];
        if (hw < tw) return 1;
        if (hw > tw) return 0;
    }
    return 1;
}

__kernel void mine_headers(__global const uchar *prefix76, uint start_nonce, uint count, __global const uint *target, __global volatile uint *result) {
    uint gid = get_global_id(0);
    if (gid >= count) return;
    if (result[0] != 0) return;
    uint nonce = start_nonce + gid;
    uint w[64];
    uint st[8];

    st[0]=0x6a09e667; st[1]=0xbb67ae85; st[2]=0x3c6ef372; st[3]=0xa54ff53a;
    st[4]=0x510e527f; st[5]=0x9b05688c; st[6]=0x1f83d9ab; st[7]=0x5be0cd19;

    for (int i=0; i<16; i++) w[i] = be32(prefix76, i*4);
    compress(w, st);

    w[0] = be32(prefix76, 64);
    w[1] = be32(prefix76, 68);
    w[2] = be32(prefix76, 72);
    w[3] = bswap32(nonce);
    w[4] = 0x80000000;
    for (int i=5; i<15; i++) w[i]=0;
    w[15] = 640;
    compress(w, st);

    uint st2[8];
    st2[0]=0x6a09e667; st2[1]=0xbb67ae85; st2[2]=0x3c6ef372; st2[3]=0xa54ff53a;
    st2[4]=0x510e527f; st2[5]=0x9b05688c; st2[6]=0x1f83d9ab; st2[7]=0x5be0cd19;
    for (int i=0; i<8; i++) w[i] = st[i];
    w[8] = 0x80000000;
    for (int i=9; i<15; i++) w[i]=0;
    w[15] = 256;
    compress(w, st2);

    if (hash_meets_target(st2, target)) {
        result[1] = nonce;
        result[0] = 1;
    }
}
"""
