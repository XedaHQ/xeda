- The UART stream adapter now carries words wider than 256 bits whose width is not a multiple
  of 8. It no longer wraps the byte offset past byte 31.
