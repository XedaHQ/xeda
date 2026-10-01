/* Only RTL contract checks use this hook. Ordinary C++ asserts keep their behavior. */
#ifndef XEDA_CXXRTL_EVIDENCE_H
#define XEDA_CXXRTL_EVIDENCE_H

void xeda_cxxrtl_assert(const char *condition, const char *file, int line);

#ifdef CXXRTL_ASSERT
#undef CXXRTL_ASSERT
#endif
#define CXXRTL_ASSERT(condition) \
    ((condition) ? (void)0 : xeda_cxxrtl_assert(#condition, __FILE__, __LINE__))

#endif
