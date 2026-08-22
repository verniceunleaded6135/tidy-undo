"""HWP 5.x (OLE2/CFB) text extractor - pure stdlib, no olefile."""
import struct, zlib, sys, re

def _read_cfb(path):
    d = open(path,'rb').read()
    if d[:8] != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1': raise ValueError('not CFB')
    ssz = 1 << struct.unpack_from('<H', d, 30)[0]
    mssz = 1 << struct.unpack_from('<H', d, 32)[0]
    n_fat = struct.unpack_from('<I', d, 44)[0]
    dir_start = struct.unpack_from('<I', d, 48)[0]
    mini_start = struct.unpack_from('<I', d, 60)[0]
    difat_start, n_difat = struct.unpack_from('<II', d, 68)
    def sect(i): return d[(i+1)*ssz:(i+2)*ssz]
    # DIFAT
    difat = list(struct.unpack_from('<109I', d, 76))
    s = difat_start
    while n_difat > 0 and s not in (0xFFFFFFFE, 0xFFFFFFFF):
        blk = sect(s); cnt = ssz//4 - 1
        difat += list(struct.unpack_from('<%dI'%cnt, blk, 0))
        s = struct.unpack_from('<I', blk, ssz-4)[0]; n_difat -= 1
    fat = []
    for fs in difat[:n_fat]:
        if fs >= 0xFFFFFFFE: break
        fat += list(struct.unpack_from('<%dI'%(ssz//4), sect(fs), 0))
    def chain(start):
        out=[]; c=start
        while c < 0xFFFFFFFE and len(out) < 1_000_000:
            out.append(c); c = fat[c] if c < len(fat) else 0xFFFFFFFE
        return out
    def read_chain(start, size=None):
        b = b''.join(sect(i) for i in chain(start))
        return b[:size] if size else b
    dirdata = read_chain(dir_start)
    entries=[]
    for off in range(0, len(dirdata), 128):
        e = dirdata[off:off+128]
        if len(e)<128: break
        nl = struct.unpack_from('<H', e, 64)[0]
        name = e[:max(nl-2,0)].decode('utf-16-le', 'ignore')
        typ = e[66]; sid = struct.unpack_from('<I', e, 116)[0]
        sz = struct.unpack_from('<Q', e, 120)[0]
        entries.append((name, typ, sid, sz))
    # ministream
    root = next((e for e in entries if e[1]==5), None)
    ministream = read_chain(root[2]) if root and root[2] < 0xFFFFFFFE else b''
    minifat = []
    if mini_start < 0xFFFFFFFE:
        mf = read_chain(mini_start)
        minifat = list(struct.unpack_from('<%dI'%(len(mf)//4), mf, 0))
    def read_mini(start, size):
        out=b''; c=start
        while c < 0xFFFFFFFE and len(out) < size:
            out += ministream[c*mssz:(c+1)*mssz]
            c = minifat[c] if c < len(minifat) else 0xFFFFFFFE
        return out[:size]
    def get(name):
        for n,t,sid,sz in entries:
            if n==name and t==2:
                return read_mini(sid,sz) if sz < 4096 else read_chain(sid,sz)
        return None
    return get, entries

def extract_text(path, max_sections=3):
    get, entries = _read_cfb(path)
    fh = get('FileHeader')
    flags = struct.unpack_from('<I', fh, 36)[0] if fh else 1
    if flags & 0x02: raise ValueError('encrypted')
    comp = bool(flags & 0x01)
    out=[]
    secs = sorted(n for n,t,_,_ in entries if t==2 and n.startswith('Section'))
    for name in secs[:max_sections]:
        raw = get(name)
        if raw is None: continue
        try: data = zlib.decompress(raw, -15) if comp else raw
        except Exception: continue
        # parse HWP records; tag 67 = HWPTAG_PARA_TEXT
        i=0
        while i+4 <= len(data):
            hdr = struct.unpack_from('<I', data, i)[0]
            tag = hdr & 0x3FF; size = (hdr >> 20) & 0xFFF
            i += 4
            if size == 0xFFF:
                size = struct.unpack_from('<I', data, i)[0]; i += 4
            if tag == 67:
                chunk = data[i:i+size]
                s=''
                j=0
                while j+2 <= len(chunk):
                    c = struct.unpack_from('<H', chunk, j)[0]; j += 2
                    if c in (0,10,13): s += '\n'
                    elif c < 32:
                        if c in (1,2,3,11,12,14,15,16,17,18,21,22,23): j += 14
                    else: s += chr(c)
                out.append(s)
            i += size
    return re.sub(r'\n{3,}', '\n\n', ''.join(out))

if __name__ == '__main__':
    print(extract_text(sys.argv[1])[:int(sys.argv[2]) if len(sys.argv)>2 else 500])
