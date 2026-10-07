- Text such as `[]` or `[a,b]` for a list setting is now an error that names the right spellings,
  `flags=` (the empty list) and `flags=a,b`. Before, `-s flags=[]` gave the one item `[]`.
