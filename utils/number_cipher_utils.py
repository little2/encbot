class NumberCipher:

    MASK32 = 0xffffffff


    def __init__(self, key=987654321):
        self.key = key



    def _mix(self, x, salt, round_no):
        """
        内部混淆函数
        """

        x = x + self.key + salt + round_no * 7919

        x ^= (x << 13) & self.MASK32
        x ^= (x >> 17)

        x *= 2654435761
        x &= self.MASK32

        return x



    def encrypt(self, number, salt=None):
        """
        数字加密

        同一个 number
        不同 salt
        会产生不同 token
        """

        if salt is None:
            # 不使用 import 的情况下
            # 使用时间产生变化
            import time
            salt = int(time.time() * 1000000) & 0xffff


        left = (number >> 32) & self.MASK32
        right = number & self.MASK32


        for i in range(8):

            left, right = (
                right,
                left ^ self._mix(right, salt, i)
            )


        encrypted = (
            (left << 32)
            |
            right
        )


        # 保存 salt
        token = (
            (salt << 64)
            |
            encrypted
        )


        return token



    def decrypt(self, token):
        """
        Token 解密
        """

        # Tokens embedded in URLs are parsed from path segments and therefore
        # arrive as decimal strings. Normalize them before using bit operations.
        token = int(token)

        salt = token >> 64


        encrypted = token & ((1 << 64)-1)


        left = encrypted >> 32
        right = encrypted & self.MASK32


        for i in range(7, -1, -1):

            left, right = (
                right ^ self._mix(left, salt, i),
                left
            )


        number = (
            (left << 32)
            |
            right
        )


        return number
