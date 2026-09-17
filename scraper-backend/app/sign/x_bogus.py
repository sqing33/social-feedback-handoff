"""TikTok X-Bogus + X-Gnarly 签名算法

复制自 TikTokDownloader (src/encrypt/xBogus.py + xGnarly.py)，移除 src.custom 依赖。
"""

from base64 import b64encode
from hashlib import md5
from random import randint
from time import time
from urllib.parse import quote, urlencode

USERAGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

__all__ = ["XBogus", "XGnarly"]


class XBogus:
    __string = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
    __array = (
        [None for _ in range(48)]
        + list(range(10))
        + [None for _ in range(39)]
        + list(range(10, 16))
    )
    __canvas = 3873194319

    @staticmethod
    def disturb_array(a, b, e, d, c, f, t, n, o, i, r, _, x, u, s, l, v, h, g):
        array = [0] * 19
        array[0] = a
        array[10] = b
        array[1] = e
        array[11] = d
        array[2] = c
        array[12] = f
        array[3] = t
        array[13] = n
        array[4] = o
        array[14] = i
        array[5] = r
        array[15] = _
        array[6] = x
        array[16] = u
        array[7] = s
        array[17] = l
        array[8] = v
        array[18] = h
        array[9] = g
        return array

    @staticmethod
    def generate_garbled_1(a, b, e, d, c, f, t, n, o, i, r, _, x, u, s, l, v, h, g):
        array = [0] * 19
        array[0] = a
        array[1] = r
        array[2] = b
        array[3] = _
        array[4] = e
        array[5] = x
        array[6] = d
        array[7] = u
        array[8] = c
        array[9] = s
        array[10] = f
        array[11] = l
        array[12] = t
        array[13] = v
        array[14] = n
        array[15] = h
        array[16] = o
        array[17] = g
        array[18] = i
        return "".join(map(chr, map(int, array)))

    @staticmethod
    def generate_num(text):
        return [
            ord(text[i]) << 16 | ord(text[i + 1]) << 8 | ord(text[i + 2]) << 0
            for i in range(0, 21, 3)
        ]

    @staticmethod
    def generate_garbled_2(a, b, c):
        return chr(a) + chr(b) + c

    @staticmethod
    def generate_garbled_3(a, b):
        d = list(range(256))
        c = 0
        f = ""
        for a_idx in range(256):
            d[a_idx] = a_idx
        for b_idx in range(256):
            c = (c + d[b_idx] + ord(a[b_idx % len(a)])) % 256
            e = d[b_idx]
            d[b_idx] = d[c]
            d[c] = e
        t = 0
        c = 0
        for b_idx in range(len(b)):
            t = (t + 1) % 256
            c = (c + d[t]) % 256
            e = d[t]
            d[t] = d[c]
            d[c] = e
            f += chr(ord(b[b_idx]) ^ d[(d[t] + d[c]) % 256])
        return f

    def calculate_md5(self, input_string):
        if isinstance(input_string, str):
            array = self.md5_to_array(input_string)
        elif isinstance(input_string, list):
            array = input_string
        else:
            raise TypeError
        md5_hash = md5()
        md5_hash.update(bytes(array))
        return md5_hash.hexdigest()

    def md5_to_array(self, md5_str):
        if isinstance(md5_str, str) and len(md5_str) > 32:
            return [ord(char) for char in md5_str]
        else:
            return [
                (self.__array[ord(md5_str[index])] << 4)
                | self.__array[ord(md5_str[index + 1])]
                for index in range(0, len(md5_str), 2)
            ]

    def process_url_path(self, url_path):
        return self.md5_to_array(
            self.calculate_md5(self.md5_to_array(self.calculate_md5(url_path)))
        )

    def generate_str(self, num):
        string = [num & 16515072, num & 258048, num & 4032, num & 63]
        string = [i >> j for i, j in zip(string, range(18, -1, -6))]
        return "".join([self.__string[i] for i in string])

    @staticmethod
    def handle_ua(a, b):
        d = list(range(256))
        c = 0
        result = bytearray(len(b))
        for i in range(256):
            c = (c + d[i] + ord(a[i % len(a)])) % 256
            d[i], d[c] = d[c], d[i]
        t = 0
        c = 0
        for i in range(len(b)):
            t = (t + 1) % 256
            c = (c + d[t]) % 256
            d[t], d[c] = d[c], d[t]
            result[i] = b[i] ^ d[(d[t] + d[c]) % 256]
        return result

    def generate_ua_array(self, user_agent, params):
        ua_key = ["\u0000", "\u0001", chr(params)]
        value = self.handle_ua(ua_key, user_agent.encode("utf-8"))
        value = b64encode(value)
        return list(md5(value).digest())

    def generate_x_bogus(self, query, params, user_agent, timestamp):
        ua_array = self.generate_ua_array(user_agent, params)
        array = [
            64, 0.00390625, 1, params,
            query[-2], query[-1], 69, 63,
            ua_array[-2], ua_array[-1],
            timestamp >> 24 & 255, timestamp >> 16 & 255,
            timestamp >> 8 & 255, timestamp >> 0 & 255,
            self.__canvas >> 24 & 255, self.__canvas >> 16 & 255,
            self.__canvas >> 8 & 255, self.__canvas >> 0 & 255,
            None,
        ]
        zero = 0
        for i in array[:-1]:
            if isinstance(i, float):
                i = int(i)
            zero ^= i
        array[-1] = zero
        garbled = self.generate_garbled_1(*self.disturb_array(*array))
        garbled = self.generate_garbled_2(2, 255, self.generate_garbled_3("ÿ", garbled))
        return "".join(self.generate_str(i) for i in self.generate_num(garbled))

    def get_x_bogus(self, query, params=8, user_agent=USERAGENT, test_time=None):
        timestamp = int(test_time or time())
        query = self.process_url_path(
            urlencode(query, quote_via=quote) if isinstance(query, dict) else query
        )
        return self.generate_x_bogus(query, params, user_agent, timestamp)


class XGnarly:
    _AA = [
        0xFFFFFFFF, 138, 1498001188, 211147047, 253, None, 203, 288, 9,
        1196819126, 3212677781, 135, 263, 193, 58, 18, 244, 2931180889, 240,
        173, 268, 2157053261, 261, 175, 14, 5, 171, 270, 156, 258, 13, 15,
        3732962506, 185, 169, 2, 6, 132, 162, 200, 3, 160, 217618912, 62,
        2517678443, 44, 164, 4, 96, 183, 2903579748, 3863347763, 119, 181,
        10, 190, 8, 2654435769, 259, 104, 230, 128, 2633865432, 225, 1, 257,
        143, 179, 16, 600974999, 185100057, 32, 188, 53, 2718276124, 177,
        196, 4294967296, 147, 117, 17, 49, 7, 28, 12, 266, 216, 11, 0, 45,
        166, 247, 1451689750,
    ]
    _OT = [_AA[9], _AA[69], _AA[51], _AA[92]]
    _MASK32 = 0xFFFFFFFF
    _BASE64_ALPHABET = (
        "u09tbS3UvgDEe6r-ZVMXzLpsAohTn7mdINQlW412GqBjfYiyk8JORCF5/xKHwacP="
    )

    def __init__(self):
        self.St = None
        self._init_prng_state()

    def _init_prng_state(self):
        now_ms = int(time() * 1000)
        self.kt = [
            self._AA[44], self._AA[74], self._AA[10], self._AA[62],
            self._AA[42], self._AA[17], self._AA[2], self._AA[21],
            self._AA[3], self._AA[70], self._AA[50], self._AA[32],
            self._AA[0] & now_ms,
            randint(0, self._AA[77]),
            randint(0, self._AA[77]),
            randint(0, self._AA[77]),
        ]
        self.St = self._AA[88]

    @classmethod
    def _u32(cls, x):
        return x & cls._MASK32

    @classmethod
    def _rotl(cls, x, n):
        return cls._u32(((x << n) & cls._MASK32) | (x >> (32 - n)))

    @classmethod
    def _quarter(cls, st, a, b, c, d):
        st[a] = cls._u32(st[a] + st[b])
        st[d] = cls._rotl(st[d] ^ st[a], 16)
        st[c] = cls._u32(st[c] + st[d])
        st[b] = cls._rotl(st[b] ^ st[c], 12)
        st[a] = cls._u32(st[a] + st[b])
        st[d] = cls._rotl(st[d] ^ st[a], 8)
        st[c] = cls._u32(st[c] + st[d])
        st[b] = cls._rotl(st[b] ^ st[c], 7)

    @classmethod
    def _chacha_block(cls, state, rounds):
        w = state.copy()
        r = 0
        while r < rounds:
            cls._quarter(w, 0, 4, 8, 12)
            cls._quarter(w, 1, 5, 9, 13)
            cls._quarter(w, 2, 6, 10, 14)
            cls._quarter(w, 3, 7, 11, 15)
            r += 1
            if r >= rounds:
                break
            cls._quarter(w, 0, 5, 10, 15)
            cls._quarter(w, 1, 6, 11, 12)
            cls._quarter(w, 2, 7, 12, 13)
            cls._quarter(w, 3, 4, 13, 14)
            r += 1
        for i in range(16):
            w[i] = cls._u32(w[i] + state[i])
        return w

    def _bump_counter(self):
        self.kt[12] = self._u32(self.kt[12] + 1)

    def rand(self):
        e = self._chacha_block(self.kt, 8)
        t = e[self.St]
        r = (e[self.St + 8] & 0xFFFFFFF0) >> 11
        if self.St == 7:
            self._bump_counter()
            self.St = 0
        else:
            self.St += 1
        return (t + 4294967296 * r) / (2**53)

    @staticmethod
    def _num_to_bytes(val):
        if val < 65535:
            return [(val >> 8) & 0xFF, val & 0xFF]
        return [(val >> 24) & 0xFF, (val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF]

    @staticmethod
    def _be_int_from_str(s):
        b = s.encode("utf-8")[:4]
        acc = 0
        for x in b:
            acc = (acc << 8) | x
        return acc & XGnarly._MASK32

    def _encrypt_chacha(self, key_words, rounds, data):
        n_full = len(data) // 4
        leftover = len(data) % 4
        words = [0] * ((len(data) + 3) // 4)
        for i in range(n_full):
            j = 4 * i
            words[i] = data[j] | (data[j + 1] << 8) | (data[j + 2] << 16) | (data[j + 3] << 24)
        if leftover:
            v = 0
            base = 4 * n_full
            for c in range(leftover):
                v |= data[base + c] << (8 * c)
            words[n_full] = v
        o = 0
        state = key_words.copy()
        while o + 16 < len(words):
            stream = self._chacha_block(state, rounds)
            state[12] = self._u32(state[12] + 1)
            for k in range(16):
                words[o + k] ^= stream[k]
            o += 16
        if o < len(words):
            stream = self._chacha_block(state, rounds)
            for k in range(len(words) - o):
                words[o + k] ^= stream[k]
        for i in range(n_full):
            w = words[i]
            j = 4 * i
            data[j:j + 4] = [w & 0xFF, (w >> 8) & 0xFF, (w >> 16) & 0xFF, (w >> 24) & 0xFF]
        if leftover:
            w = words[n_full]
            base = 4 * n_full
            for c in range(leftover):
                data[base + c] = (w >> (8 * c)) & 0xFF

    def _ab22(self, key12_words, rounds, s):
        state = self._OT + key12_words
        data = [ord(ch) for ch in s]
        self._encrypt_chacha(state, rounds, data)
        return "".join(chr(x) for x in data)

    def generate(self, query_string, body="", user_agent=USERAGENT, envcode=0, version="5.1.1"):
        timestamp_ms = int(time() * 1000)
        obj = {
            1: 1, 2: envcode,
            3: md5(query_string.encode()).hexdigest(),
            4: md5(body.encode()).hexdigest(),
            5: md5(user_agent.encode()).hexdigest(),
            6: timestamp_ms // 1000,
            7: 1508145731,
            8: int((timestamp_ms * 1000) % 2147483648),
            9: version,
        }
        if version == "5.1.1":
            obj[10] = "1.0.0.314"
            obj[11] = 1
            v12 = 0
            for i in range(1, 12):
                v = obj[i]
                to_xor = v if isinstance(v, int) else self._be_int_from_str(v)
                v12 ^= to_xor
            obj[12] = v12 & self._MASK32
        elif version != "5.1.0":
            raise ValueError(f"Unsupported version: {version}")
        v0 = 0
        for i in range(1, len(obj) + 1):
            v = obj[i]
            if isinstance(v, int):
                v0 ^= v
        obj[0] = v0 & self._MASK32
        payload = [len(obj)]
        for k, v in obj.items():
            payload.append(k)
            val_bytes = self._num_to_bytes(v) if isinstance(v, int) else list(v.encode("utf-8"))
            payload.extend(self._num_to_bytes(len(val_bytes)))
            payload.extend(val_bytes)
        base_str = "".join(chr(x) for x in payload)
        key_words = []
        key_bytes = []
        round_accum = 0
        for _ in range(12):
            word = int(self.rand() * 4294967296) & self._MASK32
            key_words.append(word)
            round_accum = (round_accum + (word & 15)) & 15
            key_bytes.extend([word & 0xFF, (word >> 8) & 0xFF, (word >> 16) & 0xFF, (word >> 24) & 0xFF])
        rounds = round_accum + 5
        enc = self._ab22(key_words, rounds, base_str)
        insert_pos = 0
        for b in key_bytes:
            insert_pos = (insert_pos + b) % (len(enc) + 1)
        for ch in enc:
            insert_pos = (insert_pos + ord(ch)) % (len(enc) + 1)
        key_bytes_str = "".join(chr(b) for b in key_bytes)
        final_str = (
            chr(((1 << 6) ^ (1 << 3) ^ 3) & 0xFF)
            + enc[:insert_pos]
            + key_bytes_str
            + enc[insert_pos:]
        )
        out = []
        full_len = (len(final_str) // 3) * 3
        for i in range(0, full_len, 3):
            block = (ord(final_str[i]) << 16) | (ord(final_str[i + 1]) << 8) | ord(final_str[i + 2])
            out.extend([
                self._BASE64_ALPHABET[(block >> 18) & 63],
                self._BASE64_ALPHABET[(block >> 12) & 63],
                self._BASE64_ALPHABET[(block >> 6) & 63],
                self._BASE64_ALPHABET[block & 63],
            ])
        return "".join(out)
