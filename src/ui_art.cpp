#include "ui_art.h"
#include "crypto.h"
#include <algorithm>
#include <charconv>
#include <cmath>

namespace b2 {
namespace {
struct Point {float x,y;};
struct Edge {Point a,b;};
struct Rect {unsigned x,y,w,h;};

// Traced in the original atlas's pixel coordinates. These are the original
// condensed lettering and symbols, not a replacement typeface. Black outlines and
// shadows still come from the game's original text draws.
struct Glyph {Rect bounds;float x,y;std::string_view path;};
constexpr Glyph letter(unsigned x,unsigned y,unsigned w,unsigned h,std::string_view path) {
    return {{x-1,y-1,w+2,h+2},float(x),float(y),path};
}
constexpr Glyph glyphs[]={
    {{154,65,14,23},155,66,
     "M6 0 C10.2 0 12 1.8 12 5 L12 16 C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 L0 5 C0 1.8 1.8 0 6 0 Z "
     "M5 4 C5 2.7 7 2.7 7 4 L7 17 C7 18.3 5 18.3 5 17 Z"},
    {{167,65,10,23},168,66,
     "M5.7 0 L8 0 L8 21 L3 21 L3 5.7 C2 5.3 1 5.1 0 5 L0 3 C2.5 2.7 4.6 1.3 5.7 0 Z"},
    {{176,65,13,23},177,66,
     "M5.5 0 C9.1 0 11 1.7 11 5.7 C11 9.3 8.5 12.5 5 18 L11 18 L11 21 L0 21 L0 19.4 "
     "C0 17.3 6 9.3 6 6.1 L6 4.1 C6 2.7 4 2.7 4 4.1 L4 7 L0 7 L0 4.7 C0 1.7 1.8 0 5.5 0 Z"},
    {{188,65,13,23},189,66,
     "M5.4 0 C9.1 0 11 1.6 11 5 L11 6.5 C11 8 10.1 9 8.6 9.5 C10.3 10 11 11.1 11 13 L11 16 "
     "C11 19.5 9.3 21 5.5 21 C1.7 21 0 19.5 0 16 L0 12 L4 12 L4 17 C4 18.5 6 18.5 6 17 L6 12 "
     "C6 10.9 5.4 10.8 3.5 10.8 L3.5 8 C5.4 8 6 7.8 6 6.7 L6 4 C6 2.7 4 2.7 4 4 L4 7 L0 7 L0 4.8 C0 1.6 1.7 0 5.4 0 Z"},
    {{200,65,14,23},200,66,
     "M4.5 0 L11 0 L11 12 L13 12 L13 17 L11 17 L11 21 L6 21 L6 17 L1 17 L1 12 Z M6 3 L3.1 12 L6 12 Z"},
    {{213,65,13,23},214,66,
     "M0 0 L11 0 L11 3 L5 3 L5 7.5 C6 6 8 5.8 9.4 6.8 C10.6 7.5 11 8.9 11 11 L11 16.3 "
     "C11 19.5 9.2 21 5.5 21 C1.8 21 0 19.5 0 16 L0 13 L4 13 L4 17 C4 18.5 6 18.5 6 17 L6 11 "
     "C6 9.4 4 9.3 4 11 L0 11 Z"},
    {{225,65,14,23},226,66,
     "M6 0 C10.1 0 12 1.8 12 5 L12 6 L7 6 L7 4 C7 2.7 5 2.7 5 4 L5 9 "
     "C6 7.4 7 7 8.4 7 C10.9 7 12 8.9 12 12 L12 16 C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 "
     "L0 5 C0 1.8 1.8 0 6 0 Z M5 11.7 L5 17 C5 18.4 7 18.4 7 17 L7 11.7 C7 10.3 5 10.3 5 11.7 Z"},
    {{238,65,12,23},239,66,
     "M0 0 L10 0 L10 3 L6 21 L1 21 L5.8 3 L0 3 Z"},
    {{0,95,14,23},1,96,
     "M6 0 C10.2 0 12 1.8 12 5 L12 6.6 C12 8.2 11.5 9 10.2 9.5 C11.5 10.2 12 11 12 12.5 L12 16 "
     "C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 L0 12.5 C0 11 0.5 10.2 1.8 9.5 C0.5 9 0 8.2 0 6.6 "
     "L0 5 C0 1.8 1.8 0 6 0 Z M5 4 C5 2.7 7 2.7 7 4 L7 7 C7 8.4 5 8.4 5 7 Z "
     "M5 12 C5 10.6 7 10.6 7 12 L7 17 C7 18.4 5 18.4 5 17 Z"},
    {{13,95,14,23},14,96,
     "M6 0 C10.2 0 12 1.8 12 5 L12 16 C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 L0 15 L5 15 "
     "L5 17 C5 18.4 7 18.4 7 17 L7 12 C6 13.4 5 14 3.6 14 C1.1 14 0 12.1 0 9 L0 5 C0 1.8 1.8 0 6 0 Z "
     "M5 4 L5 9.4 C5 10.8 7 10.8 7 9.4 L7 4 C7 2.7 5 2.7 5 4 Z"},
    {{140,184,11,24},0,0,"M146.1 185 L150.1 185 L144.7 207 L140.7 207 Z"},
    letter(1,9,12,18, // a
        "M6 0 C10.2 0 12 1.6 12 5 L12 18 L7 18 L7 16.5 C6 17.6 4.8 18 3.5 18 C1.2 18 0 16.4 0 13 L0 12 "
        "C0 8.4 3.3 8 7 6.9 L7 4 C7 2.5 5 2.5 5 4 L5 7 L0 7 L0 5 C0 1.6 1.8 0 6 0 Z "
        "M7 9 C5.5 9.5 5 10 5 11.4 L5 14 C5 15.4 7 15.4 7 14 Z"),
    letter(14,6,12,21, // b
        "M0 0 L5 0 L5 4.5 C6.2 3.4 7.3 3 8.5 3 C11 3 12 4.8 12 8 L12 16 C12 19.3 11 21 8.5 21 "
        "C7 21 5.9 20.3 5 19.3 L5 21 L0 21 Z M5 7 L5 17 C5 18.4 7 18.4 7 17 L7 7 C7 5.6 5 5.6 5 7 Z"),
    letter(27,9,11,18, // c
        "M5.5 0 C9.3 0 11 1.6 11 5 L11 7 L7 7 L7 4 C7 2.6 5 2.6 5 4 L5 14 C5 15.4 7 15.4 7 14 L7 10 "
        "L11 10 L11 13 C11 16.4 9.2 18 5.5 18 C1.7 18 0 16.4 0 13 L0 5 C0 1.6 1.7 0 5.5 0 Z"),
    letter(39,6,12,21, // d
        "M7 0 L12 0 L12 21 L7 21 L7 19.4 C5.9 20.5 4.9 21 3.5 21 C1 21 0 19.3 0 16 L0 8 C0 4.8 1 3 3.5 3 "
        "C4.9 3 6 3.6 7 4.6 Z M5 7 L5 17 C5 18.4 7 18.4 7 17 L7 7 C7 5.6 5 5.6 5 7 Z"),
    letter(52,9,12,18, // e
        "M6 0 C10.2 0 12 1.7 12 5 L12 9 L5 9 L5 14 C5 15.4 7 15.4 7 14 L7 10 L12 10 L12 13 "
        "C12 16.4 10.2 18 6 18 C1.8 18 0 16.4 0 13 L0 5 C0 1.7 1.8 0 6 0 Z M5 4 L5 6 L7 6 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(65,6,8,21, // f
        "M5 0 L8 0 L8 3 L7 3 C6.1 3 6 3.3 6 4 L8 4 L8 7 L6 7 L6 21 L1 21 L1 7 L0 7 L0 4 L1 4 C1 1.3 2.3 0 5 0 Z"),
    letter(74,9,12,20, // g
        "M3.5 0 C5.2 0 6.2 .6 7 1.8 L7 0 L12 0 L12 15 C12 18.4 10.2 20 6 20 C2 20 0 18.6 0 16 L5 16 "
        "C5 17.4 7 17.4 7 16 L7 13.4 C5.9 14.5 4.9 15 3.5 15 C1 15 0 13.3 0 10 L0 5 C0 1.7 1 0 3.5 0 Z "
        "M5 4 L5 11 C5 12.4 7 12.4 7 11 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(87,6,12,21, // h
        "M0 0 L5 0 L5 4.5 C6 3.5 7.3 3 8.5 3 C11 3 12 4.8 12 8 L12 21 L7 21 L7 7 C7 5.6 5 5.6 5 7 L5 21 L0 21 Z"),
    letter(100,6,5,21,"M0 0 L5 0 L5 3 L0 3 Z M0 4 L5 4 L5 21 L0 21 Z"), // i
    letter(106,6,6,23, // j
        "M1 0 L6 0 L6 3 L1 3 Z M1 4 L6 4 L6 19 C6 21.8 5 23 2 23 L0 23 L0 20 C.8 20 1 19.8 1 19 Z"),
    letter(113,6,12,21, // k
        "M0 0 L5 0 L5 9 L8.5 3 L12 3 L8 10.5 L12 21 L8 21 L5 12.5 L5 21 L0 21 Z"),
    letter(126,6,5,21,"M0 0 L5 0 L5 21 L0 21 Z"), // l
    letter(132,9,19,18, // m
        "M0 0 L5 0 L5 1.5 C6.2 .4 7.1 0 8.5 0 C10 0 11 .6 11.8 1.9 C13.2 .5 14.3 0 15.5 0 "
        "C18 0 19 1.8 19 5 L19 18 L14 18 L14 4 C14 2.6 12 2.6 12 4 L12 18 L7 18 L7 4 C7 2.6 5 2.6 5 4 L5 18 L0 18 Z"),
    letter(152,9,12,18, // n
        "M0 0 L5 0 L5 1.5 C6.2 .4 7.3 0 8.5 0 C11 0 12 1.8 12 5 L12 18 L7 18 L7 4 C7 2.6 5 2.6 5 4 L5 18 L0 18 Z"),
    letter(165,9,12,18, // o
        "M6 0 C10.2 0 12 1.7 12 5 L12 13 C12 16.4 10.2 18 6 18 C1.8 18 0 16.4 0 13 L0 5 C0 1.7 1.8 0 6 0 Z "
        "M5 4 L5 14 C5 15.4 7 15.4 7 14 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(178,9,12,20, // p
        "M0 0 L5 0 L5 1.6 C6 .5 7.2 0 8.5 0 C11 0 12 1.8 12 5 L12 13 C12 16.3 11 18 8.5 18 "
        "C7 18 6 17.4 5 16.3 L5 20 L0 20 Z M5 4 L5 14 C5 15.4 7 15.4 7 14 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(191,9,12,20, // q
        "M3.5 0 C4.9 0 6 .6 7 1.7 L7 0 L12 0 L12 20 L7 20 L7 16.4 C6 17.5 4.9 18 3.5 18 C1 18 0 16.3 0 13 "
        "L0 5 C0 1.7 1 0 3.5 0 Z M5 4 L5 14 C5 15.4 7 15.4 7 14 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(204,9,8,18, // r
        "M0 0 L5 0 L5 2 C5.8 .7 6.7 0 8 0 L8 5 L7 5 C5.4 5 5 5.7 5 7 L5 18 L0 18 Z"),
    letter(213,9,11,18, // s
        "M5.5 0 C9.3 0 11 1.6 11 5 L11 6 L7 6 L7 4 C7 2.6 5 2.6 5 4 L5 4.7 C5 6.5 7.4 7.4 9.1 8.8 "
        "C10.5 10 11 11 11 13.3 C11 16.5 9.4 18 5.5 18 C1.7 18 0 16.4 0 13 L0 12 L4 12 L4 14 "
        "C4 15.4 6 15.4 6 14 L6 13.5 C6 11.7 3.6 10.9 1.9 9.4 C.5 8.2 0 7.1 0 4.8 C0 1.6 1.7 0 5.5 0 Z"),
    letter(225,7,8,20, // t
        "M1 0 L6 0 L6 3 L8 3 L8 6 L6 6 L6 16 C6 16.8 6.2 17 7 17 L8 17 L8 20 L5 20 C2.2 20 1 18.8 1 16 L1 6 L0 6 L0 3 L1 3 Z"),
    letter(234,9,12,18, // u
        "M0 0 L5 0 L5 14 C5 15.4 7 15.4 7 14 L7 0 L12 0 L12 18 L7 18 L7 16.5 C5.9 17.6 4.9 18 3.5 18 C1 18 0 16.3 0 13 Z"),
    letter(1,39,13,18,"M0 0 L5 0 L6.5 13 L8 0 L13 0 L10 18 L3 18 Z"), // v
    letter(15,39,17,18, // w
        "M0 0 L4 0 L5.3 11.2 L7 0 L10 0 L11.7 11.2 L13 0 L17 0 L14.5 18 L10 18 L8.5 9.5 L7 18 L2.5 18 Z"),
    letter(33,39,13,18,"M0 0 L5 0 L6.5 6 L8 0 L13 0 L9.5 9 L13 18 L8 18 L6.5 12 L5 18 L0 18 L3.5 9 Z"), // x
    letter(47,39,12,20, // y
        "M0 0 L4.8 0 L6.5 13.2 L7.3 0 L11.7 0 L10 16 C9.5 19 8.8 20 6.4 20 L1 20 L1 17 L3.4 17 C3.9 17 4.1 16.8 4 16.2 Z"),
    letter(60,39,9,18,"M0 0 L9 0 L9 3 L5.1 15 L9 15 L9 18 L0 18 L0 15 L3.9 3 L0 3 Z"), // z
    letter(71,36,13,21, // A
        "M3 0 L10 0 L13 21 L8.5 21 L8 17 L4.5 17 L4 21 L0 21 Z M6.2 3.4 L4.8 13 L7.6 13 Z"),
    letter(86,36,12,21, // B
        "M0 0 L7.7 0 C10.8 0 12 1.7 12 5 L12 6.7 C12 8.1 11.3 9.1 9.7 9.6 C11.3 10 12 11.2 12 13 L12 16 "
        "C12 19.4 10.7 21 7.7 21 L0 21 Z M5 3 L5 8 L5.8 8 C6.8 8 7 7.7 7 6.8 L7 4.2 C7 3.2 6.7 3 5.8 3 Z "
        "M5 12 L5 18 L5.8 18 C6.7 18 7 17.7 7 16.8 L7 13.2 C7 12.2 6.8 12 5.8 12 Z"),
    letter(99,36,12,21, // C
        "M6 0 C10.2 0 12 1.8 12 5 L12 9 L7 9 L7 4 C7 2.6 5 2.6 5 4 L5 17 C5 18.4 7 18.4 7 17 L7 12 L12 12 "
        "L12 16 C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 L0 5 C0 1.8 1.8 0 6 0 Z"),
    letter(112,36,12,21, // D
        "M0 0 L7.5 0 C10.7 0 12 1.8 12 5 L12 16 C12 19.3 10.7 21 7.5 21 L0 21 Z "
        "M5 3 L5 18 L5.7 18 C6.7 18 7 17.7 7 16.5 L7 4.5 C7 3.3 6.7 3 5.7 3 Z"),
    letter(125,36,9,21,"M0 0 L8.5 0 L8.5 4 L5 4 L5 8 L8.5 8 L8.5 12 L5 12 L5 17 L9 17 L9 21 L0 21 Z"), // E
    letter(135,36,9,21,"M0 0 L9 0 L9 4 L5 4 L5 8 L9 8 L9 12 L5 12 L5 21 L0 21 Z"), // F
    letter(145,36,12,21, // G
        "M6 0 C10.2 0 12 1.8 12 5 L12 8 L7 8 L7 4 C7 2.6 5 2.6 5 4 L5 17 C5 18.4 7 18.4 7 17 L7 13 L6 13 "
        "L6 10 L12 10 L12 21 L9.5 21 L9.2 19.5 C8.2 20.6 7.3 21 5.8 21 C1.8 21 0 19.4 0 16 L0 5 C0 1.8 1.8 0 6 0 Z"),
    letter(158,36,12,21,"M0 0 L5 0 L5 8 L7 8 L7 0 L12 0 L12 21 L7 21 L7 12 L5 12 L5 21 L0 21 Z"), // H
    letter(171,36,5,21,"M0 0 L5 0 L5 21 L0 21 Z"), // I
    letter(177,36,7,21,"M2 0 L7 0 L7 16 C7 19.4 5.4 21 2 21 L0 21 L0 17 C1.7 17 2 16.7 2 15 Z"), // J
    letter(185,36,12,21,"M0 0 L5 0 L5 8 L7.5 0 L12 0 L12 1.3 L9.6 9.5 C9.4 9.8 9.4 10.2 9.6 10.5 L12 19.7 L12 21 L7.5 21 L5 12 L5 21 L0 21 Z"), // K
    letter(199,36,8,21,"M0 0 L5 0 L5 17 L8 17 L8 21 L0 21 Z"), // L
    letter(208,36,16,21,"M0 0 L6.8 0 L8.3 9 L10 0 L16 0 L16 21 L11 21 L11 8 L9.4 21 L7.2 21 L5 8 L5 21 L0 21 Z"), // M
    letter(225,36,12,21,"M0 0 L4 0 L7 10 L7 0 L12 0 L12 21 L8 21 L5 11 L5 21 L0 21 Z"), // N
    letter(238,36,12,21, // O
        "M6 0 C10.2 0 12 1.8 12 5 L12 16 C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 L0 5 C0 1.8 1.8 0 6 0 Z "
        "M5 4 L5 17 C5 18.4 7 18.4 7 17 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(1,66,12,21, // P
        "M0 0 L7.5 0 C10.7 0 12 1.8 12 5 L12 8 C12 11.3 10.7 13 7.5 13 L5 13 L5 21 L0 21 Z "
        "M5 3 L5 10 L5.7 10 C6.7 10 7 9.7 7 8.5 L7 4.5 C7 3.3 6.7 3 5.7 3 Z"),
    letter(14,66,12,23, // Q
        "M6 0 C10.2 0 12 1.8 12 5 L12 16 C12 18.3 11 19.8 8.8 20.5 C9 21 9.6 21 12 21 L12 23 L8.3 23 "
        "C5.5 23 4.9 22.5 4.4 20.9 C1.4 20.5 0 19 0 16 L0 5 C0 1.8 1.8 0 6 0 Z "
        "M5 4 L5 17 C5 18.4 7 18.4 7 17 L7 4 C7 2.6 5 2.6 5 4 Z"),
    letter(27,66,12,21, // R
        "M0 0 L7.5 0 C10.7 0 12 1.8 12 5 L12 6.7 C12 9 11 10.2 8.9 10.6 C11.3 10.8 12 11.9 12 14 L12 21 "
        "L7 21 L7 13.5 C7 12.2 6.7 12 5 12 L5 21 L0 21 Z M5 3 L5 8.8 L5.7 8.8 C6.7 8.8 7 8.4 7 7.2 L7 4.5 C7 3.3 6.7 3 5.7 3 Z"),
    letter(40,66,13,21, // S
        "M6 0 C10.1 0 12 1.8 12 5 L12 6 L7 6 L7 4 C7 2.6 5 2.6 5 4 L5 4.5 C5 6.2 7.4 7.3 9.9 9.5 "
        "C12.3 11.6 13 12.8 13 15.8 C13 19.4 10.8 21 6.5 21 C2.1 21 0 19.5 0 16 L0 14 L5 14 L5 17 "
        "C5 18.4 8 18.4 8 17 L8 16 C8 14.2 5.2 12.8 3.2 11 C.8 8.8 0 7.7 0 4.8 C0 1.6 1.8 0 6 0 Z"),
    letter(54,66,11,21,"M0 0 L11 0 L11 4 L8 4 L8 21 L3 21 L3 4 L0 4 Z"), // T
    letter(66,66,12,21,"M0 0 L5 0 L5 17 C5 18.4 7 18.4 7 17 L7 0 L12 0 L12 16 C12 19.4 10.2 21 6 21 C1.8 21 0 19.4 0 16 Z"), // U
    letter(79,66,13,21,"M0 0 L5 0 L6.5 14 L8 0 L13 0 L10 21 L3 21 Z"), // V
    letter(93,66,21,21,"M0 0 L5 0 L6.4 13 L8 0 L12.5 0 L14.4 13 L16 0 L21 0 L18.2 21 L12.5 21 L10.4 10.5 L8.4 21 L2.8 21 Z"), // W
    letter(115,66,14,21,"M0 0 L4.3 0 L6.6 7.1 L9.2 0 L14 0 L9.6 10 L13.7 21 L9 21 L6.6 13.5 L4.2 21 L0 21 L3.8 10 Z"), // X
    letter(130,66,13,21,"M0 0 L5 0 L6.5 8.6 L8 0 L13 0 L9 13 L9 21 L4 21 L4 13 Z"), // Y
    letter(144,66,10,21,"M1 0 L10 0 L10 4 L5 17 L10 17 L10 21 L0 21 L0 17 L5 4 L1 4 Z"), // Z
    letter(181,186,5,21,"M0 0 L5 0 L4 16 L1 16 Z M0 17 L5 17 L5 21 L0 21 Z"), // !
    letter(127,191,11,12,"M2 0 L5.5 3.8 L9 0 L11 2 L7.6 6 L11 10 L9 12 L5.5 8.2 L2 12 L0 10 L3.4 6 L0 2 Z"), // small multiplication x
    letter(234,189,16,18, // &
        "M6.5 0 C10.2 0 12 1.6 12 4.6 C12 6.5 11 7.7 9.7 8.9 L13 12 "
        "C14.1 11.5 15 10.5 16 9.5 L16 13.9 L14.7 15.1 L16 17 L16 18 L11.2 18 L9.7 16.7 "
        "C8.2 17.7 6.9 18 5.3 18 C2 18 0 16.3 0 13 C0 11.7 .4 10.4 2.7 8.7 "
        "C1.6 7.2 1 6.1 1 4.1 C1 1.4 3.1 0 6.5 0 Z "
        "M6 4 L6 4.5 C6 5.5 6.4 6.4 7 7 C7.6 6.4 8 5.5 8 4.5 L8 4 C8 2.7 6 2.7 6 4 Z "
        "M5.4 12 C5 12.6 5 13.4 5.2 14.1 C5.5 15 6.5 15.1 7.8 14.9 L8.1 14.5 L6 11.6 Z"),
    letter(247,6,7,21, // (
        "M7 0 L7 3 L5.8 3 C5.2 3 5 3.3 5 4 L5 17 C5 17.7 5.2 18 5.8 18 L7 18 L7 21 L4 21 "
        "C1.1 21 0 20 0 17 L0 4 C0 1 1.1 0 4 0 Z"),
    letter(132,216,7,21, // )
        "M0 0 L3 0 C5.9 0 7 1 7 4 L7 17 C7 20 5.9 21 3 21 L0 21 L0 18 L1.2 18 "
        "C1.8 18 2 17.7 2 17 L2 4 C2 3.3 1.8 3 1.2 3 L0 3 Z"),
    letter(221,184,12,25, // $
        "M5 0 L7 0 L7 2.1 C10.5 2.4 12 3.9 12 7 L12 8 L7 8 L7 6 C7 4.5 5 4.5 5 6 L5 7.6 "
        "C5 8.8 8.5 9.1 10.4 11 C11.6 12.4 12 14 12 17.2 C12 20.9 10.3 22.7 7 22.9 L7 25 L5 25 L5 22.9 "
        "C1.4 22.6 0 21 0 17.8 L0 15 L5 15 L5 19 C5 20.5 7 20.5 7 19 L7 18 C7 15 4.6 13.6 2.3 11.9 "
        "C.6 10.4 0 9.5 0 7 C0 4 1.5 2.4 5 2.1 Z"),
    // Remaining symbols in the original character table. Append to preserve the
    // digit/alphabet indices used by the gauges. Shared crops also cover aliases.
    letter(41,215,9,7, // double quote
        "M0 0 L4 0 L4 4 L3 7 L1 7 L0 4 Z M5 0 L9 0 L9 4 L8 7 L6 7 L5 4 Z"),
    letter(152,186,19,21, // %
        "M3.5 0 C5.9 0 7 1.3 7 3 L7 9 C7 11 5.9 12 3.5 12 C1.1 12 0 11 0 9 L0 3 C0 1.3 1.1 0 3.5 0 Z "
        "M3 2.5 L3 9.5 C3 10.2 4 10.2 4 9.5 L4 2.5 C4 1.8 3 1.8 3 2.5 Z "
        "M13.6 0 L15.5 0 L5 21 L3.1 21 Z "
        "M15.5 9 C17.9 9 19 10.3 19 12 L19 18 C19 20 17.9 21 15.5 21 C13.1 21 12 20 12 18 L12 12 C12 10.3 13.1 9 15.5 9 Z "
        "M15 11.5 L15 18.5 C15 19.2 16 19.2 16 18.5 L16 11.5 C16 10.8 15 10.8 15 11.5 Z"),
    letter(251,185,4,7,"M0 0 L4 0 L4 4 L3 7 L1 7 L0 4 Z"), // apostrophe
    letter(233,215,14,14, // asterisk
        "M4 0 L8 0 L8 4.5 L12.1 3.1 L13.4 6.5 L9.1 8.1 L11.7 11.8 L8.7 14 L6 10.3 "
        "L3.3 14 L.3 11.8 L2.9 8.1 L-1.4 6.5 L-.1 3.1 L4 4.5 Z"),
    letter(90,190,13,13,"M5 0 L8 0 L8 5 L13 5 L13 8 L8 8 L8 13 L5 13 L5 8 L0 8 L0 5 L5 5 Z"), // +
    letter(251,53,4,7,"M0 0 L4 0 L4 3 C4 5 2.6 6.5 0 7 L0 5.5 C1.4 5 2 4.5 2 4 L0 4 Z"), // comma
    letter(104,196,8,4,"M0 0 L8 0 L8 4 L0 4 Z"), // hyphen
    letter(250,83,4,4,"M0 0 L4 0 L4 4 L0 4 Z"), // period
    letter(250,133,4,14,"M0 0 L4 0 L4 4 L0 4 Z M0 10 L4 10 L4 14 L0 14 Z"), // colon
    letter(251,163,4,17, // semicolon
        "M0 0 L4 0 L4 4 L0 4 Z M0 10 L4 10 L4 13 C4 15 2.6 16.5 0 17 L0 15.5 C1.4 15 2 14.5 2 14 L0 14 Z"),
    letter(15,220,12,13,"M12 0 L12 3.3 L4.5 6.5 L12 9.7 L12 13 L0 7.9 L0 5.1 Z"), // <
    letter(113,192,12,8,"M0 0 L12 0 L12 3 L0 3 Z M0 5 L12 5 L12 8 L0 8 Z"), // =
    letter(28,220,12,13,"M0 0 L12 5.1 L12 7.9 L0 13 L0 9.7 L7.5 6.5 L0 3.3 Z"), // >
    letter(187,186,13,21, // ?
        "M6.5 0 C10.8 0 13 2.1 13 6 L13 10 C13 13.4 11.7 15 8.8 15 C7.4 15 6.1 14.8 5 14 "
        "L5 16 L0 16 L0 10 L5 10 L5 11 C5 12.4 8 12.4 8 11 L8 6 C8 4.7 7.4 4 6.5 4 "
        "C5.6 4 5 4.7 5 6 L5 7 L0 7 L0 6 C0 2.1 2.2 0 6.5 0 Z M0 17 L5 17 L5 21 L0 21 Z"),
    letter(1,216,6,21,"M0 0 L6 0 L6 3 L5 3 L5 18 L6 18 L6 21 L0 21 Z"), // [
    letter(8,216,6,21,"M0 0 L6 0 L6 21 L0 21 L0 18 L1 18 L1 3 L0 3 Z"), // ]
    letter(173,211,59,29,"M0 0 L57 0 L28.5 29 Z"), // down arrow (character 0x5E)
    letter(51,239,15,1,"M0 0 L15 0 L15 1 L0 1 Z"), // underscore
    letter(81,192,8,8, // bullet
        "M4 0 C6.21 0 8 1.79 8 4 C8 6.21 6.21 8 4 8 C1.79 8 0 6.21 0 4 C0 1.79 1.79 0 4 0 Z"),
    letter(111,216,20,11, // trademark
        "M0 0 L8 0 L8 2 L5 2 L5 11 L3 11 L3 2 L0 2 Z "
        "M9 0 L12.5 0 L14.5 7.5 L16.5 0 L20 0 L20 11 L18 11 L18 2.5 L15.5 11 L13.5 11 L11 2.5 L11 11 L9 11 Z"),
    letter(201,188,5,22,"M0 0 L5 0 L5 4 L0 4 Z M.7 5 L4.3 5 L5 22 L0 22 Z"), // inverted !
    letter(67,217,21,20, // copyright
        "M10.5 0 C16.3 0 21 4.48 21 10 C21 15.52 16.3 20 10.5 20 C4.7 20 0 15.52 0 10 C0 4.48 4.7 0 10.5 0 Z "
        "M10.5 2 C5.81 2 2 5.58 2 10 C2 14.42 5.81 18 10.5 18 C15.19 18 19 14.42 19 10 C19 5.58 15.19 2 10.5 2 Z "
        "M11 5 C13.5 5 15.3 6.4 15.8 8.6 L13.8 9 C13.4 7.5 12.5 6.8 11 6.8 C8.8 6.8 7 8.3 7 10.5 "
        "C7 12.9 8.5 14.2 10.8 14.2 C12.3 14.2 13.4 13.6 14 12.6 L15.8 13.4 C15 15.1 13.2 16 10.8 16 "
        "C7.3 16 5 13.8 5 10.5 C5 7.2 7.5 5 11 5 Z"),
    letter(140,211,32,29, // right arrow (character 0xAC)
        "M5 0 L18 0 L32 14.5 L18 29 L5 29 L14.5 19 L0 19 L0 10 L14.5 10 Z"),
    letter(89,217,21,20, // registered trademark
        "M10.5 0 C16.3 0 21 4.48 21 10 C21 15.52 16.3 20 10.5 20 C4.7 20 0 15.52 0 10 C0 4.48 4.7 0 10.5 0 Z "
        "M10.5 2 C5.81 2 2 5.58 2 10 C2 14.42 5.81 18 10.5 18 C15.19 18 19 14.42 19 10 C19 5.58 15.19 2 10.5 2 Z "
        "M6 5 L11 5 C14.2 5 15 6.2 15 8 C15 9.8 14 10.7 12 11 L15 15 L12.5 15 L9.5 11 L8 11 L8 15 L6 15 Z "
        "M8 7 L8 9 L11.2 9 C12.5 9 13 8.7 13 8 C13 7.3 12.5 7 11.2 7 Z"),
    letter(172,185,8,8, // degree (also ordinal 0xBA)
        "M4 0 C6.21 0 8 1.79 8 4 C8 6.21 6.21 8 4 8 C1.79 8 0 6.21 0 4 C0 1.79 1.79 0 4 0 Z "
        "M4 2 C2.9 2 2 2.9 2 4 C2 5.1 2.9 6 4 6 C5.1 6 6 5.1 6 4 C6 2.9 5.1 2 4 2 Z"),
    letter(207,188,13,22, // inverted ?
        "M8 0 L13 0 L13 4 L8 4 Z M8 5 L13 5 L13 11 L8 11 C8 9.7 5 9.7 5 11 L5 16 "
        "C5 17.4 5.5 18 6.5 18 C7.5 18 8 17.4 8 16 L8 14 L13 14 L13 16 C13 20 10.8 22 6.5 22 "
        "C2.2 22 0 20 0 16 L0 11 C0 7.8 1.5 6 4.3 6 C5.8 6 7 6.3 8 7.2 Z")
};

// Convert the recognized BC3 atlases without a GPU readback. Other source
// artwork keeps its original colors/alpha and bilinear reconstruction.
std::vector<std::byte> decode(const TextureData& t) {
    std::vector<std::byte> pixels(std::size_t(t.width)*t.height*4);
    for(unsigned y=0;y<t.height;y+=4) for(unsigned x=0;x<t.width;x+=4) {
        const auto block=t.bytes.subspan((std::size_t(y/4)*(t.width/4)+x/4)*16,16);
        unsigned alpha[8]={unsigned(block[0]),unsigned(block[1])};
        if(alpha[0]>alpha[1]) for(unsigned i=2;i<8;++i) alpha[i]=((8-i)*alpha[0]+(i-1)*alpha[1])/7;
        else {for(unsigned i=2;i<6;++i) alpha[i]=((6-i)*alpha[0]+(i-1)*alpha[1])/5;alpha[6]=0;alpha[7]=255;}
        unsigned colors[4][3]{};
        for(unsigned i=0;i<2;++i) {
            const unsigned c=u16(block,8+i*2),r=(c>>11)&31,g=(c>>5)&63,b=c&31;
            colors[i][0]=(b<<3)|(b>>2);colors[i][1]=(g<<2)|(g>>4);colors[i][2]=(r<<3)|(r>>2);
        }
        for(unsigned c=0;c<3;++c) {colors[2][c]=(2*colors[0][c]+colors[1][c])/3;colors[3][c]=(colors[0][c]+2*colors[1][c])/3;}
        std::uint64_t indices=0;std::memcpy(&indices,block.data()+2,6);const auto color=u32(block,12);
        for(unsigned i=0;i<16;++i) {
            const auto p=(std::size_t(y+i/4)*t.width+x+i%4)*4;
            for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte(colors[(color>>(i*2))&3][c]);
            pixels[p+3]=std::byte(alpha[(indices>>(i*3))&7]);
        }
    }
    return pixels;
}

std::vector<Edge> outline(std::string_view path,float x,float y) {
    std::vector<Edge> edges;edges.reserve(256);Point current{},start{};
    const auto number=[&]() {
        while(!path.empty() && path.front()==' ') path.remove_prefix(1);
        float value;const auto result=std::from_chars(path.data(),path.data()+path.size(),value);
        if(result.ec!=std::errc{}) throw std::runtime_error("Invalid built-in UI outline");
        path.remove_prefix(result.ptr-path.data());return value;
    };
    const auto point=[&]() {const auto a=number(),b=number();return Point{a+x,b+y};};
    const auto line=[&](Point p) {if(p.x!=current.x || p.y!=current.y) edges.push_back({current,p});current=p;};
    while(!path.empty()) {
        const auto command=path.front();path.remove_prefix(1);
        if(command==' ') continue;
        if(command=='M') current=start=point();
        else if(command=='L') line(point());
        else if(command=='Z') line(start);
        else if(command=='C') {
            const auto a=current,b=point(),c=point(),d=point();
            for(unsigned i=1;i<=24;++i) {const float t=i/24.f,s=1-t;
                line({s*s*s*a.x+3*s*s*t*b.x+3*s*t*t*c.x+t*t*t*d.x,
                      s*s*s*a.y+3*s*s*t*b.y+3*s*t*t*c.y+t*t*t*d.y});}
        } else throw std::runtime_error("Unknown built-in UI outline command");
    }
    return edges;
}

void paint(std::vector<std::byte>& pixels,unsigned width,Rect rect,const std::vector<Edge>& edges,
           std::uint32_t ink=0xFFFFFFFF,bool over=false,bool nonzero=false) {
    const unsigned w=rect.w*ui_art_scale,h=rect.h*ui_art_scale;
    struct Crossing {float x;int direction;};
    std::vector<float> coverage(w);std::vector<Crossing> crossings;crossings.reserve(edges.size());
    struct Span {const Edge* edge;float low,high;};
    std::vector<Span> spans;spans.reserve(edges.size());
    for(const auto& edge:edges) if(edge.a.y!=edge.b.y)
        spans.push_back({&edge,std::min(edge.a.y,edge.b.y),std::max(edge.a.y,edge.b.y)});
    std::ranges::sort(spans,{},&Span::low);
    std::vector<Span> active;active.reserve(spans.size());std::size_t next=0;
    // Two vertical subsamples plus exact horizontal pixel coverage. Rasterize
    // only during atlas upload; gameplay uses an ordinary immutable texture.
    for(unsigned row=0;row<h;++row) {
        std::ranges::fill(coverage,0.f);
        for(unsigned sample=0;sample<2;++sample) {
            const auto y=rect.y+(row*2+sample+.5f)/(2*ui_art_scale);crossings.clear();
            std::erase_if(active,[&](const Span& span){return span.high<=y;});
            while(next<spans.size() && spans[next].low<=y) {
                if(spans[next].high>y) active.push_back(spans[next]);
                ++next;
            }
            for(const auto& span:active) {
                const auto& e=*span.edge;
                crossings.push_back({(e.a.x+(y-e.a.y)*(e.b.x-e.a.x)/(e.b.y-e.a.y)-rect.x)*ui_art_scale,e.b.y>e.a.y?1:-1});
            }
            std::ranges::sort(crossings,{},&Crossing::x);int winding=0;
            for(std::size_t i=0;i+1<crossings.size();++i) {
                winding+=crossings[i].direction;
                if(nonzero?winding==0:(i&1)!=0) continue;
                const auto left=std::clamp(crossings[i].x,0.f,float(w)),right=std::clamp(crossings[i+1].x,0.f,float(w));
                for(unsigned col=unsigned(left);col<unsigned(std::ceil(right));++col)
                    coverage[col]+=.5f*(std::min(right,float(col+1))-std::max(left,float(col)));
            }
        }
        for(unsigned x=0;x<w;++x) {
            const auto p=(std::size_t(rect.y*ui_art_scale+row)*width+rect.x*ui_art_scale+x)*4;
            const float a=std::clamp(coverage[x],0.f,1.f)*float(ink>>24)/255;
            if(over) {
                if(a==0) continue;
                const float background=unsigned(pixels[p+3])*(1-a)/255,alpha=a+background;
                for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte(unsigned(std::lround(
                    (((ink>>(c*8))&255)*a+unsigned(pixels[p+c])*background)/alpha)));
                pixels[p+3]=std::byte(unsigned(std::lround(alpha*255)));
            } else {
                for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte((ink>>(c*8))&255);
                pixels[p+3]=std::byte(unsigned(std::lround(a*255)));
            }
        }
    }
}

std::vector<std::byte> enlarge(Bytes source,unsigned w,unsigned h,bool font) {
    const unsigned width=w*ui_art_scale,height=h*ui_art_scale,channels=font?1:4;
    std::vector<std::byte> pixels(std::size_t(width)*height*4,std::byte{255});
    // At 8x, bilinear weights are exact sixteenths. Expand each source row
    // once, retaining only the two rows needed by vertical interpolation.
    struct Sample {unsigned left,right,weight;};
    std::vector<Sample> columns(width);
    for(unsigned x=0;x<width;++x) {
        const int left=int((x+ui_art_scale/2)/ui_art_scale)-1;
        columns[x]={unsigned(std::clamp(left,0,int(w)-1))*4,unsigned(std::clamp(left+1,0,int(w)-1))*4,
                    ((x+ui_art_scale/2)%ui_art_scale)*2+1};
    }
    const unsigned stride=width*channels;
    std::vector<std::uint16_t> rows(stride*2);int cached[2]={-1,-1};
    const auto row=[&](unsigned y) {
        auto* output=rows.data()+(y%2)*stride;
        if(cached[y%2]!=int(y)) {
            const auto* input=source.data()+std::size_t(y)*w*4;
            for(unsigned x=0;x<width;++x) {
                const auto s=columns[x];
                for(unsigned c=0;c<channels;++c) {
                    const unsigned channel=font?3:c;
                    output[x*channels+c]=std::uint16_t(unsigned(input[s.left+channel])*(16-s.weight)+unsigned(input[s.right+channel])*s.weight);
                }
            }
            cached[y%2]=int(y);
        }
        return output;
    };
    for(unsigned y=0;y<height;++y) {
        const int top=int((y+ui_art_scale/2)/ui_art_scale)-1;
        const unsigned weight=((y+ui_art_scale/2)%ui_art_scale)*2+1;
        const auto* a=row(unsigned(std::clamp(top,0,int(h)-1)));
        const auto* b=row(unsigned(std::clamp(top+1,0,int(h)-1)));
        auto* output=pixels.data()+std::size_t(y)*width*4;
        if(font) for(unsigned x=0;x<width;++x) output[x*4+3]=std::byte((a[x]*(16-weight)+b[x]*weight+128)>>8);
        else for(unsigned i=0;i<stride;++i) output[i]=std::byte((a[i]*(16-weight)+b[i]*weight+128)>>8);
    }
    return pixels;
}

std::vector<std::byte> compress_font(Bytes pixels,unsigned width,unsigned height) {
    // The verified atlas is white RGB everywhere. BC3 needs only its alpha
    // endpoints/selectors plus constant white color blocks: 4 MiB, not 16 MiB.
    std::vector<std::byte> blocks(std::size_t(width)*height);
    for(unsigned y=0;y<height;y+=4) for(unsigned x=0;x<width;x+=4) {
        unsigned alpha[16],low=255,high=0;
        for(unsigned i=0;i<16;++i) {
            alpha[i]=unsigned(pixels[(std::size_t(y+i/4)*width+x+i%4)*4+3]);
            low=std::min(low,alpha[i]);high=std::max(high,alpha[i]);
        }
        std::uint64_t indices=0;
        if(high!=low) for(unsigned i=0;i<16;++i) {
            const unsigned step=((high-alpha[i])*7+(high-low)/2)/(high-low);
            const unsigned index=step==0?0:step==7?1:step+1;
            indices|=std::uint64_t(index)<<(i*3);
        }
        auto* block=blocks.data()+(std::size_t(y/4)*(width/4)+x/4)*16;
        block[0]=std::byte(high);block[1]=std::byte(low);std::memcpy(block+2,&indices,6);
        constexpr std::uint64_t white=0x00000000FFFFFFFFULL;std::memcpy(block+8,&white,8);
    }
    return blocks;
}

using Color=std::array<float,3>;
// Smooth RGB controls fitted to each original dome independently. Constrained
// channel order and nonnegative Bernstein weights keep each button inside its
// own color gamut, avoiding colored fringes from polynomial overshoot.
constexpr Color button_shades[4][36]={
    { // A
        {0.0f,255.0f,0.0f},
        {58.7f,75.7f,58.7f},
        {255.0f,255.0f,255.0f},
        {0.0f,255.0f,104.2f},
        {0.0f,255.0f,11.8f},
        {0.0f,29.9f,29.9f},
        {0.0f,205.9f,0.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {0.0f,255.0f,80.0f},
        {0.0f,150.3f,20.7f},
        {0.0f,88.5f,88.5f},
        {132.8f,175.7f,175.7f},
        {255.0f,255.0f,255.0f},
        {240.0f,240.0f,240.0f},
        {0.0f,255.0f,0.0f},
        {0.0f,57.3f,0.0f},
        {0.0f,0.0f,0.0f},
        {113.0f,189.1f,135.2f},
        {255.0f,255.0f,255.0f},
        {0.0f,255.0f,96.3f},
        {0.0f,255.0f,0.0f},
        {0.0f,188.3f,188.3f},
        {0.0f,10.1f,10.1f},
        {0.0f,255.0f,0.0f},
        {0.0f,212.0f,66.1f},
        {0.0f,255.0f,0.0f},
        {0.0f,155.4f,86.5f},
        {0.0f,0.0f,0.0f},
        {0.0f,4.4f,4.4f},
        {0.0f,214.1f,10.2f},
        {0.0f,116.3f,116.3f},
        {0.0f,55.8f,55.8f},
        {0.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {8.5f,21.5f,21.5f}
    },
    { // B
        {43.1f,0.0f,0.0f},
        {255.0f,0.0f,0.0f},
        {218.6f,0.0f,214.8f},
        {255.0f,0.0f,0.0f},
        {255.0f,0.0f,0.0f},
        {64.9f,0.0f,0.0f},
        {255.0f,0.0f,87.7f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,81.1f,173.5f},
        {255.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {255.0f,13.4f,184.8f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,0.0f,0.0f},
        {244.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {220.8f,0.0f,0.0f},
        {255.0f,95.7f,255.0f},
        {255.0f,0.0f,9.8f},
        {255.0f,0.0f,0.0f},
        {179.5f,0.0f,0.0f},
        {19.5f,0.0f,0.0f},
        {219.3f,0.0f,0.0f},
        {255.0f,0.0f,0.0f},
        {255.0f,0.0f,0.0f},
        {255.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {8.4f,8.4f,8.4f},
        {38.4f,0.0f,0.0f},
        {236.5f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {28.3f,28.3f,28.3f}
    },
    { // X
        {0.0f,0.0f,77.1f},
        {0.0f,255.0f,255.0f},
        {3.6f,255.0f,255.0f},
        {0.0f,255.0f,255.0f},
        {0.0f,0.0f,147.4f},
        {0.0f,0.0f,0.0f},
        {0.0f,210.3f,255.0f},
        {0.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {53.1f,255.0f,255.0f},
        {0.0f,147.0f,255.0f},
        {0.0f,0.0f,0.0f},
        {0.0f,255.0f,255.0f},
        {180.5f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {0.0f,255.0f,255.0f},
        {0.0f,58.9f,231.4f},
        {0.0f,0.0f,78.0f},
        {0.0f,92.3f,186.2f},
        {0.0f,255.0f,255.0f},
        {0.0f,255.0f,255.0f},
        {0.0f,255.0f,255.0f},
        {0.0f,0.0f,202.7f},
        {0.0f,18.0f,30.9f},
        {0.0f,0.0f,106.1f},
        {0.0f,255.0f,255.0f},
        {0.0f,119.3f,255.0f},
        {0.0f,0.0f,191.8f},
        {0.0f,0.0f,0.0f},
        {0.0f,12.9f,38.7f},
        {0.0f,0.0f,0.0f},
        {0.0f,0.0f,112.1f},
        {0.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {23.6f,36.8f,106.8f}
    },
    { // Y
        {191.1f,107.9f,0.0f},
        {255.0f,255.0f,159.4f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,46.9f},
        {255.0f,92.9f,0.0f},
        {0.0f,0.0f,0.0f},
        {255.0f,255.0f,11.9f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,0.0f},
        {118.9f,0.0f,0.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,197.6f,0.0f},
        {173.4f,52.3f,52.3f},
        {241.5f,169.4f,20.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,255.0f},
        {255.0f,255.0f,0.0f},
        {215.7f,0.0f,0.0f},
        {40.4f,40.4f,13.7f},
        {255.0f,255.0f,0.0f},
        {255.0f,255.0f,227.3f},
        {255.0f,255.0f,0.0f},
        {255.0f,81.3f,0.0f},
        {0.0f,0.0f,0.0f},
        {121.4f,0.0f,0.0f},
        {137.4f,54.8f,0.0f},
        {255.0f,255.0f,0.0f},
        {252.5f,142.5f,69.7f},
        {129.0f,0.0f,0.0f},
        {0.0f,0.0f,0.0f},
        {255.0f,178.1f,178.1f}
    }
};

std::array<float,6> button_weights(float t) {
    const float s=1-t,t2=t*t,s2=s*s;
    return {s2*s2*s,5*t*s2*s2,10*t2*s2*s,10*t2*t*s2,5*t2*t2*s,t2*t2*t};
}

template<class Shade>
void ellipse(std::vector<std::byte>& pixels,unsigned width,float cx,float cy,float rx,float ry,Shade shade) {
    const unsigned left=unsigned(std::floor(cx-rx))*ui_art_scale,top=unsigned(std::floor(cy-ry))*ui_art_scale;
    const unsigned right=unsigned(std::ceil(cx+rx))*ui_art_scale,bottom=unsigned(std::ceil(cy+ry))*ui_art_scale;
    for(unsigned y=top;y<bottom;++y) for(unsigned x=left;x<right;++x) {
        const float u=((x+.5f)/ui_art_scale-cx)/rx,v=((y+.5f)/ui_art_scale-cy)/ry;
        const float a=std::clamp((1-std::sqrt(u*u+v*v))*std::min(rx,ry)*ui_art_scale+.5f,0.f,1.f);
        if(a==0) continue;
        const auto color=shade(u,v);const auto p=(std::size_t(y)*width+x)*4;
        const float background=unsigned(pixels[p+3])*(1-a)/255,alpha=a+background;
        for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte(unsigned(std::lround(
            (std::clamp(color[2-c],0.f,255.f)*a+unsigned(pixels[p+c])*background)/alpha)));
        pixels[p+3]=std::byte(unsigned(std::lround(alpha*255)));
    }
}

void buttons(std::vector<std::byte>& pixels,unsigned width) {
    // Original 128x128 buttons_Xbox layout, including START/BACK and L/R.
    // Smooth color patches fit the source's unlettered dome pixels. Outlines,
    // lettering, highlights and rims are geometry, never enlarged pixel edges.
    constexpr Point centers[]={{17,17},{51,17},{85,17},{80,111}};
    for(unsigned button=0;button<4;++button) {
        // Collapse each row of the color patch once, outside the pixel loop.
        std::array<std::array<Color,6>,32*ui_art_scale> rows{};
        for(unsigned y=0;y<rows.size();++y) {
            const auto weights=button_weights((y+.5f)/rows.size());
            for(unsigned x=0;x<6;++x) for(unsigned j=0;j<6;++j) for(unsigned c=0;c<3;++c)
                rows[y][x][c]+=button_shades[button][j*6+x][c]*weights[j];
        }
        ellipse(pixels,width,centers[button].x,centers[button].y,16,16,[&](float u,float v) {
            const auto weights=button_weights((u+1)*.5f);
            const auto& row=rows[std::min(unsigned((v+1)*.5f*rows.size()),unsigned(rows.size()-1))];
            Color color{};for(unsigned i=0;i<6;++i) for(unsigned c=0;c<3;++c) color[c]+=row[i][c]*weights[i];return color;
        });
    }
    const auto shape=[&](Rect bounds,float x,float y,std::string_view path,std::uint32_t color=0xFF000000) {
        paint(pixels,width,bounds,outline(path,x,y),color,true);
    };
    shape({7,6,20,22},0,0, // A
        "M15.2 7 L17.3 7 L26 27 L23.8 27 L21.1 20.8 L11.5 20.8 L9 27 L7 27 Z M16.2 9.5 L12.3 18.9 L20.3 18.9 Z");
    shape({44,6,16,22},0,0, // B
        "M44.7 7 L51.6 7 C55.7 7 58 8.6 58 12 C58 14.3 56.7 15.8 54.7 16.4 "
        "C57.7 16.8 59 18.5 59 21.2 C59 25.2 56.2 27 51.4 27 L44.7 27 Z "
        "M46.5 8.8 L46.5 15.5 L51.5 15.5 C54.8 15.5 56.2 14.5 56.2 12.1 C56.2 9.8 54.8 8.8 51.5 8.8 Z "
        "M46.5 17.4 L46.5 25.1 L51.3 25.1 C55.2 25.1 57 24 57 21.2 C57 18.5 55.2 17.4 51.3 17.4 Z");
    shape({76,6,19,22},0,0, // X
        "M77.5 7 L79.8 7 L85.5 15.2 L91.4 7.7 L93.5 7.7 L86.6 16.6 L94 27 L91.7 27 L85.4 18.1 L78.3 27 L76.2 27 L84.3 16.7 Z");
    shape({70,100,19,22},0,0, // Y
        "M70.8 101 L73.1 101 L79.3 110.5 L86 101 L88.1 101 L80.5 112.2 L80.5 121 L78 121 L78 112.2 Z");

    const auto black=[](float,float){return Color{0,0,0};};
    const auto white=[](float,float){return Color{250,250,250};};
    const auto rim=[](float u,float v) {const float value=std::clamp(22+30*(u+v),8.f,80.f);return Color{value,value,value};};
    const auto lip=[](float u,float v) {const float value=std::clamp(90+120*(u+v),25.f,235.f);return Color{value,value,value};};
    for(const float cx:{23.5f,67.5f}) {
        ellipse(pixels,width,cx,57,10.5f,10.8f,rim);
        ellipse(pixels,width,cx-.5f,56.5f,9.3f,9.5f,lip);
        ellipse(pixels,width,cx-1,55.9f,8.5f,8.8f,black);
        ellipse(pixels,width,cx-3.6f,53.6f,1.7f,2.2f,white);
    }
    ellipse(pixels,width,16,111,15.1f,15.2f,rim);
    ellipse(pixels,width,15.3f,111,13.7f,13.7f,lip);
    ellipse(pixels,width,14.6f,110.3f,12.8f,12.8f,black);
    ellipse(pixels,width,14,110,12.6f,12.1f,white);
    ellipse(pixels,width,10.1f,105.6f,2.7f,3.4f,black);
    ellipse(pixels,width,47,111,15.4f,15.2f,rim);
    ellipse(pixels,width,46,110.6f,13.8f,13.8f,lip);
    ellipse(pixels,width,45.1f,109.8f,12.8f,12.8f,black);
    ellipse(pixels,width,40.5f,105.6f,2.6f,3.2f,white);

    shape({1,34,47,12},0,0, // START, original bold legend
        "M5.7 35 C8.3 35 9.7 36.2 9.7 38 L7.6 38 C7.6 37.1 6.9 36.6 5.7 36.6 C4.6 36.6 4 37.1 4 37.8 "
        "C4 38.6 5.2 38.9 6.8 39.3 C9 39.8 9.8 40.6 9.8 42.1 C9.8 44.2 8.2 45.2 5.7 45.2 "
        "C3 45.2 1.6 43.9 1.6 41.9 L3.8 41.9 C3.8 43 4.5 43.6 5.8 43.6 C7 43.6 7.6 43.1 7.6 42.3 "
        "C7.6 41.5 6.7 41.2 5.1 40.8 C3 40.3 1.9 39.5 1.9 38 C1.9 36.1 3.3 35 5.7 35 Z "
        "M11 35 L19 35 L19 37 L16 37 L16 45 L14 45 L14 37 L11 37 Z "
        "M21.6 35 L24 35 L28 45.3 L25.8 45.3 L25 43 L20.6 43 L19.8 45.3 L17.8 45.3 Z M22.8 37.1 L21.3 41 L24.3 41 Z "
        "M29 35 L34 35 C36.8 35 38 36 38 38 C38 39.5 37.2 40.4 35.9 40.7 L38.2 45.3 L35.9 45.3 L33.7 41 L31 41 L31 45.3 L29 45.3 Z "
        "M31 36.8 L31 39.3 L33.9 39.3 C35.3 39.3 35.9 39 35.9 38 C35.9 37.1 35.3 36.8 33.9 36.8 Z "
        "M39 35 L47 35 L47 37 L44 37 L44 45 L42 45 L42 37 L39 37 Z",0xFFF4F4F4);
    shape({48,34,40,12},0,0, // BACK, original light legend
        "M49 35 L53.5 35 C55.7 35 57 35.8 57 37.5 C57 38.7 56.3 39.4 55.3 39.8 C56.7 40.1 57.3 40.8 57.3 42 "
        "C57.3 44 55.9 45 53.5 45 L49 45 Z M50.3 36.2 L50.3 39.2 L53.5 39.2 C55 39.2 55.7 38.7 55.7 37.6 C55.7 36.6 55 36.2 53.5 36.2 Z "
        "M50.3 40.4 L50.3 43.8 L53.5 43.8 C55.2 43.8 56 43.3 56 42.1 C56 40.9 55.2 40.4 53.5 40.4 Z "
        "M61.4 35 L62.8 35 L67.1 45 L65.7 45 L64.4 41.9 L59.8 41.9 L58.6 45 L57.3 45 Z M62.1 36.5 L60.3 40.7 L63.9 40.7 Z "
        "M72 34.8 C74.4 34.8 75.9 36.2 76 38 L74.7 38 C74.5 36.8 73.5 36 72 36 C69.7 36 68.5 37.4 68.5 40 "
        "C68.5 42.6 69.7 44 72 44 C73.5 44 74.5 43.2 74.7 41.8 L76 41.8 C75.8 44 74.3 45.2 72 45.2 "
        "C68.9 45.2 67.2 43.4 67.2 40 C67.2 36.6 68.9 34.8 72 34.8 Z "
        "M78 35 L79.4 35 L79.4 39.9 L84.7 35 L86.5 35 L82 39.3 L87.1 45 L85.3 45 L81 40.2 L79.4 41.7 L79.4 45 L78 45 Z",0xFFF4F4F4);

    for(const auto pos:{Point{89,35},Point{98,95}}) {
        const Rect bounds{unsigned(pos.x),unsigned(pos.y),24,32};
        shape(bounds,pos.x,pos.y,"M0 0 L24 0 L24 32 L0 32 Z");
        shape(bounds,pos.x,pos.y,
            "M0 0 L24 0 L24 1 L1 1 L1 32 L0 32 Z M2 2 L21 2 L21 3 L3 3 L3 30 L2 30 Z",0xFFFFFFFF);
    }
    shape({94,42,18,20},89,35, // R
        "M5 7 L17 7 C20.2 7 21.2 9 21.2 12 C21.2 15 19.5 16.9 16.3 17.4 L22 26 L17.4 26 L12 18 L9 18 L9 26 L5 26 Z "
        "M9 10 L9 15 L15.5 15 C16.8 15 17.2 14.1 17.2 12.5 C17.2 10.8 16.8 10 15.5 10 Z",0xFFFFFFFF);
    shape({104,102,13,19},98,95,"M6 7 L10 7 L10 23 L19 23 L19 26 L6 26 Z",0xFFFFFFFF);
}

// Gauge paths use the original 256x128 speedo and 128x128 burnbar coordinates.
// The four dial layouts share their rims, but retain each car's scale/redline.
struct Gauge {std::string_view hash;unsigned layout;float redline;unsigned face_alpha=127;};
constexpr Gauge gauges[]={
    {"536A679F312C33E997C87E5833D7333C37AFED88",0,-30}, // classic, gangsta
    {"90735F94EDE4DB605E3AA53E1D8C1F5C81A7E962",0,-5},  // comp
    {"05EF682A0ED3113BA880330740DFC5DB6666606C",2,-18},  // compcs
    {"E7612CF6C7B8411AEBD8E6C20E2D4DB2EA360300",1,-63}, // copcar
    {"1447AF2571294B7F70E017F946C18CB21F665221",0,0},   // coupe
    {"0305C1DAC8A371629370EBCDC9E31CC77356EFAE",2,-32},  // coupecs
    {"2234AB2EF1154E256D5D132018B31D5D17436D6C",1,-55}, // driveed
    {"F6AEEF982DE017137714627132645951C81FC58D",0,-52}, // hotrod, usmus
    {"2D113550B8A474133E57269643AFA28483AB1A5D",2,-32},  // jmus
    {"C2DE157690BA5AAE7C47E995B1AE4AE71CC2F399",0,-15},  // pick, road, suv
    {"BEB15E8AE42451D0308C6190C843AAAF91EF1D75",2,-3},   // pickcs, suvcs
    {"3D5E9FE9A8D2429D91C0FF76D5E9872121793666",2,3},    // roadcs
    {"8B7F3D8917AB071C301ECD76C79FD90F64B5BF66",0,-25}, // sport
    {"B6474E520CE8DDDD33EBB979271D91B3E332EC57",2,3},    // sportcs
    {"FAF69775248B56DAD4D0291D37FE5F6189A6D7B3",2,-40,116}, // stock, usmuscs
    {"572F9CFE6AFF2D408CCBA3E7AC1EED553D97631C",3,9}     // super
};

void vector_fill(std::vector<std::byte>& pixels,unsigned width,const std::vector<Edge>& edges,std::uint32_t ink,bool nonzero=false) {
    if(edges.empty()) return;
    float left=float(width/ui_art_scale),top=float(pixels.size()/4/width/ui_art_scale),right=0,bottom=0;
    for(const auto& e:edges) for(const auto p:{e.a,e.b}) {
        left=std::min(left,p.x);top=std::min(top,p.y);right=std::max(right,p.x);bottom=std::max(bottom,p.y);
    }
    const unsigned x=unsigned(std::max(0.f,std::floor(left))),y=unsigned(std::max(0.f,std::floor(top)));
    const unsigned r=unsigned(std::min(float(width/ui_art_scale),std::ceil(right)));
    const unsigned b=unsigned(std::min(float(pixels.size()/4/width/ui_art_scale),std::ceil(bottom)));
    if(r>x && b>y) paint(pixels,width,{x,y,r-x,b-y},edges,ink,true,nonzero);
}

template<class Width>
void vector_stroke_width(std::vector<std::byte>& pixels,unsigned width,const std::vector<Edge>& edges,Width thickness,std::uint32_t ink,float end_cap=0,bool round_joins=false) {
    // Offset each continuous contour, keeping joins smooth on both sides.
    // All rasterization is bounded to the stroke's own rectangle at upload.
    for(std::size_t first=0;first<edges.size();) {
        auto last=first+1;
        while(last<edges.size() && edges[last].a.x==edges[last-1].b.x && edges[last].a.y==edges[last-1].b.y) ++last;
        const bool closed=edges[last-1].b.x==edges[first].a.x && edges[last-1].b.y==edges[first].a.y;
        const auto count=last-first;std::vector<Point> normals(count),left,right;
        left.reserve(count+1);right.reserve(count+1);
        for(std::size_t i=0;i<count;++i) {
            const auto& e=edges[first+i];const float dx=e.b.x-e.a.x,dy=e.b.y-e.a.y,length=std::hypot(dx,dy);
            normals[i]={-dy/length,dx/length};
        }
        for(std::size_t i=0;i<=count;++i) {
            auto p=i==count?edges[last-1].b:edges[first+i].a;
            auto a=normals[i?i-1:closed?count-1:0],b=normals[i<count?i:closed?0:count-1];
            if(!closed && i==0) {p.x-=b.y*end_cap;p.y+=b.x*end_cap;}
            if(!closed && i==count) {p.x+=a.y*end_cap;p.y-=a.x*end_cap;}
            const float scale=thickness(p)*.5f/std::max(.25f,1+a.x*b.x+a.y*b.y);
            const Point n{(a.x+b.x)*scale,(a.y+b.y)*scale};
            const float turn=a.x*b.y-a.y*b.x;
            const auto join=[&](std::vector<Point>& side,float sign) {
                if(round_joins && turn*sign<0) {
                    const float start=std::atan2(sign*a.y,sign*a.x),angle=std::atan2(turn,a.x*b.x+a.y*b.y);
                    const auto steps=unsigned(std::ceil(std::abs(angle)*8));
                    for(unsigned j=0;j<=steps;++j) {
                        const float theta=start+angle*j/steps,radius=thickness(p)*.5f;
                        side.push_back({p.x+radius*std::cos(theta),p.y+radius*std::sin(theta)});
                    }
                } else side.push_back({p.x+sign*n.x,p.y+sign*n.y});
            };
            join(left,1);join(right,-1);
        }
        std::vector<Edge> ribbon;ribbon.reserve((count+1)*2);
        for(std::size_t i=1;i<left.size();++i) ribbon.push_back({left[i-1],left[i]});
        ribbon.push_back({left.back(),right.back()});
        for(std::size_t i=right.size()-1;i;--i) ribbon.push_back({right[i],right[i-1]});
        ribbon.push_back({right.front(),left.front()});vector_fill(pixels,width,ribbon,ink,true);first=last;
    }
}
void vector_stroke(std::vector<std::byte>& pixels,unsigned width,const std::vector<Edge>& edges,float thickness,std::uint32_t ink,float end_cap=0,bool round_joins=false) {
    vector_stroke_width(pixels,width,edges,[=](Point){return thickness;},ink,end_cap,round_joins);
}

Point radial(Point center,float radius,float degrees) {
    const float angle=degrees*.01745329252f;return {center.x+radius*std::cos(angle),center.y+radius*std::sin(angle)};
}
float distance(Point a,Point b) {
    const float x=a.x-b.x,y=a.y-b.y;return std::sqrt(x*x+y*y);
}
std::vector<Edge> arc(Point center,float radius,float start,float end) {
    // Two-degree segments deviate by less than .08 of an 8x output texel on
    // the largest rim. Finer subdivision only increases upload work.
    std::vector<Edge> result;const unsigned count=unsigned(std::ceil(std::abs(end-start)*.5f));result.reserve(count);
    auto previous=radial(center,radius,start);
    for(unsigned i=1;i<=count;++i) {const auto p=radial(center,radius,start+(end-start)*i/count);result.push_back({previous,p});previous=p;}
    return result;
}

void wreath_art(std::vector<std::byte>& pixels,unsigned width) {
    // Thirteen leaves per branch, traced from fe_parts_xg. Both branches share
    // the original mirror axis at x63; their stems cross below the number.
    constexpr std::string_view leaves[]={
        "M53.6 208.2 C53.4 205.5 55 203.7 57.8 203 C58 205.8 56.4 207.9 53.6 208.2 Z",
        "M50.7 211 C48.4 209.3 48.7 206.3 50.6 203.9 C53 206 53.8 208.6 50.7 211 Z",
        "M50.7 210 C54 208.4 57 209.1 60 210.5 C57.2 212.7 53.4 213 50.7 210 Z",
        "M47.8 214.4 C44.2 213.9 43.1 210.6 44.2 207.4 C47.3 208.5 49 211 47.8 214.4 Z",
        "M46.9 216.5 C47.9 212.9 51.9 213 54.6 213.2 C53.7 215.9 50.1 217.1 46.9 216.5 Z",
        "M45.5 218.3 C42 218.1 40.4 215.7 40.5 212.8 C43.8 212.8 46 215 45.5 218.3 Z",
        "M46.4 223 C46.8 219.3 49.4 218.2 52 217.9 C52.7 220.9 49.4 223 46.4 223 Z",
        "M46.2 223 C42.7 223.2 39.9 222.6 39.3 218.5 C42.6 218.7 45.4 219.2 46.2 223 Z",
        "M48.1 228.5 C47.8 225.1 49.3 223 52.4 222.1 C53.8 225.3 51.4 228.6 48.1 228.5 Z",
        "M47.3 227.6 C43.6 228.6 40.7 226.9 39.7 223.9 C43.3 223.5 46.2 224.3 47.3 227.6 Z",
        "M57.5 232.4 C53.6 231.7 53.4 229.7 54.1 225.8 C57.7 226.6 58.4 228.3 57.5 232.4 Z",
        "M50.8 230.9 C47.4 233 44.7 232.3 42.1 230.1 C45.1 227.9 48.5 228.7 50.8 230.9 Z",
        "M55.6 232.3 C54.5 235.3 50.6 236 47.4 234.5 C49.7 231.8 52.5 230.8 55.6 232.3 Z"
    };
    for(unsigned y=202*ui_art_scale;y<240*ui_art_scale;++y)
        std::fill_n(pixels.begin()+(std::size_t(y)*width+37*ui_art_scale)*4,52*ui_art_scale*4,std::byte{});
    for(bool mirror:{false,true}) {
        const auto traced=[=](std::string_view path) {
            auto edges=outline(path,0,0);
            if(mirror) for(auto& e:edges) for(auto* p:{&e.a,&e.b}) p->x=126-p->x;
            return edges;
        };
        const auto stem=traced("M53.4 208.8 C46.7 212.3 43.8 221.9 49.9 228.7 C54.4 233.5 64 233.9 69.3 238");
        vector_stroke(pixels,width,stem,2,0xF0080808);
        vector_stroke(pixels,width,stem,.8f,0xFF929292);
        for(auto path:leaves) {
            const auto edges=traced(path);
            vector_stroke(pixels,width,edges,1.25f,0xF0080808);
            vector_fill(pixels,width,edges,0xFFF4F4F4);
        }
    }
}

void gauge_art(std::vector<std::byte>& pixels,unsigned width,const Gauge& gauge) {
    const bool single=gauge.layout<2;
    constexpr Point tach{67.85f,64.12f},speed{66.24f,68.44f},paired{120.57f,98.25f};
    const auto fill=[&](std::string_view path,std::uint32_t ink) {vector_fill(pixels,width,outline(path,0,0),ink);};
    const auto stroke=[&](const std::vector<Edge>& path,float size,std::uint32_t ink) {vector_stroke(pixels,width,path,size,ink);};
    const auto rim=[&](Point center,float radius,float start,float end) {
        stroke(arc(center,radius-3.5f,start,end),7,0xFF080808);
        const auto shine=arc(center,radius-2.6f,start,end);
        stroke(shine,3.5f,0xFF606060);stroke(shine,2.5f,0xFFB0B0B0);
        stroke(shine,1.5f,0xFFFFFFFF);
        stroke(arc(center,radius-7.2f,start,end),1.2f,0xFFB0B0B0);
    };
    const auto shade_rim=[&](Point center,float radius) {
        // Shade each rim before its markings, so the rear dial can be completed
        // before the foreground instrument occludes it.
        const unsigned left=unsigned(std::max(0.f,std::floor(center.x-radius)))*ui_art_scale;
        const unsigned top=unsigned(std::max(0.f,std::floor(center.y-radius)))*ui_art_scale;
        const unsigned right=unsigned(std::min(174.f,std::ceil(center.x+radius)))*ui_art_scale;
        const unsigned bottom=unsigned(std::min(124.f,std::ceil(center.y+radius)))*ui_art_scale;
        for(unsigned y=single?0:top;y<(single?124*ui_art_scale:bottom);++y) {
            for(unsigned x=single?0:left;x<(single?174*ui_art_scale:right);++x) {
                const auto p=(std::size_t(y)*width+x)*4;
                if(pixels[p]==std::byte{} || pixels[p]!=pixels[p+2]) continue;
                const Point position{(x+.5f)/ui_art_scale,(y+.5f)/ui_art_scale};
                if(!single && distance(position,center)>radius) continue;
                const float angle=std::atan2(position.y-center.y,position.x-center.x)*57.2957795f;
                const float reflection=std::remainder(angle+110,360.f)/57;
                float light=.41f+.59f*std::exp(-reflection*reflection);
                if(single) light=position.x>64 && position.y>85?.6f:light+.19f*std::clamp((position.y-100)/19.5f,0.f,1.f);
                const auto shade=std::byte(unsigned(std::lround(unsigned(pixels[p])*light)));
                pixels[p]=pixels[p+1]=pixels[p+2]=shade;
            }
        }
    };
    const auto ticks=[&](Point center,float radius,float first,float step,unsigned count,bool minor) {
        for(unsigned i=0;i<count;++i) {
            const bool short_tick=minor && (i&1);
            // Align zero to the left without shifting the other scale divisions.
            const float angle=first+step*i+(single && i==0?1.5f:0.f);
            // Minor marks sit inside the ring, detached from its chrome.
            const float outer=short_tick?radius-(single?3.f:1.7f):radius;
            const std::vector<Edge> line{{radial(center,outer,angle),radial(center,outer-(short_tick?2.5f:single?6.5f:4.4f),angle)}};
            const float size=short_tick?.95f:1.35f;
            stroke(line,size+.6f,0xFF080808);
            stroke(line,size,short_tick&&!single?0xFF707070:0xFFDCDCDC);
        }
    };
    const auto redline=[&] {
        const auto red=arc(single?tach:paired,single?48.5f:41.5f,gauge.redline,single?18.f:32.f);
        stroke(red,single?8.4f:7.3f,0xFF080808);
        stroke(red,single?6.4f:5.4f,gauge.layout==3?0xFF780000:0xFFAA0000);
        // Stock's red sector shares its translucent face; the chrome, ticks
        // and lettering are still opaque. Apply this before drawing those.
        if(gauge.face_alpha<127) for(unsigned y=0;y<124*ui_art_scale;++y) for(unsigned x=0;x<174*ui_art_scale;++x) {
            auto& a=pixels[(std::size_t(y)*width+x)*4+3];a=std::min(a,std::byte(gauge.face_alpha));
        }
    };
    if(single) {
        auto body=arc(tach,60.7f,18,-256);
        auto panel=outline("M53.165 123.017 C56.2 123.77 60 123.8 64 123.8 L165.5 123.8 C167.1 123.8 168 122.8 168 121 L168 89.3 "
            "C168 87.6 166.8 87 165 87 L89 87 C68.67 87 63.78 105.86 56.84 108.27",0,0);
        panel.front().a=body.back().b;body.insert(body.end(),panel.begin(),panel.end());
        auto inner=arc(tach,45.5f,104,378);body.push_back({body.back().b,inner.front().a});
        body.insert(body.end(),inner.begin(),inner.end());body.push_back({body.back().b,body.front().a});
        vector_fill(pixels,width,body,0x82000000);
        redline();
        // Trace both original edges: the speed-window border tapers to the
        // inner dial at 104 degrees, leaving clearance beside the zero.
        fill("M56.84 108.27 C63.78 105.86 68.67 87 89 87 L165 87 "
            "C166.8 87 166.8 88 166.8 89.3 L166.8 91.06 L89 91.06 "
            "C69.99 91.06 67.76 107.68 56.84 108.27 Z",0xFF000000);
        // The original chrome is slightly offset inside the black silhouette.
        // Its lower end flows into the speed window, without a radial end cap.
        auto frame=arc({67.6f,63.2f},56.9f,18,-256);
        auto bottom=outline("M53.835 118.409 C57.5 119.3 60 119.5 64 119.5 L163.6 119.5 "
            "C165 119.5 165.5 118.6 165.5 117.4 L165.5 90",0,0);
        bottom.front().a=frame.back().b;frame.insert(frame.end(),bottom.begin(),bottom.end());
        const auto frame_ink=[&](float round,float straight,std::uint32_t ink) {
            vector_stroke_width(pixels,width,frame,[=](Point p) {
                const float t=p.x>64 && p.y>85?1.f:std::clamp((p.y-105)/14.5f,0.f,1.f);
                return round+(straight-round)*t;
            },ink);
        };
        frame_ink(7.1f,4.8f,0xFF080808);
        frame_ink(3.5f,1.6f,0xFF606060);frame_ink(2.5f,1.3f,0xFFB0B0B0);frame_ink(1.5f,.95f,0xFFFFFFFF);
        stroke(arc(tach,53.5f,104,378),1.2f,0xFFB0B0B0);
        shade_rim(tach,0);
        ticks(tach,54,104,gauge.layout==0?16.f:11.65f,gauge.layout==0?18:24,true);
    } else {
        // The small dial occludes the large dial, including its transparent
        // center. Construct the shared translucent backing once, without
        // stacking alpha where the two instruments overlap.
        for(unsigned y=0;y<124*ui_art_scale;++y) for(unsigned x=0;x<174*ui_art_scale;++x) {
            const float px=(x+.5f)/ui_art_scale,py=(y+.5f)/ui_art_scale;
            const float a=distance({px,py},speed),b=distance({px,py},paired);
            float large=std::clamp((63.5f-a)*ui_art_scale+.5f,0.f,1.f);
            if(py<100) large*=std::clamp((distance({px,py},{65,69.1f})-37.2f)*ui_art_scale+.5f,0.f,1.f);
            const float cover=std::clamp((52-b)*ui_art_scale+.5f,0.f,1.f);
            float face=cover;
            if(py<100) face*=std::max(std::clamp((distance({px,py},{120.8f,98.1f})-37.3f)*ui_art_scale+.5f,0.f,1.f),std::clamp((15-distance({px,py},{120.5f,100}))*ui_art_scale+.5f,0.f,1.f));
            pixels[(std::size_t(y)*width+x)*4+3]=std::byte(unsigned(std::lround(gauge.face_alpha*(large*(1-cover)+face))));
        }
        redline();
        rim(speed,63.5f,122,360);
        shade_rim(speed,64);
        ticks(speed,56,125,12.1f,19,true);
        // Occlude both the rear rim and its ticks at the foreground silhouette.
        for(unsigned y=46*ui_art_scale;y<124*ui_art_scale;++y) for(unsigned x=68*ui_art_scale;x<132*ui_art_scale;++x) {
            const float px=(x+.5f)/ui_art_scale,py=(y+.5f)/ui_art_scale;
            const float r=distance({px,py},paired);
            if(r<52) {
                const auto p=(std::size_t(y)*width+x)*4;
                for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte{};
                const float a=py>=100?1.f:std::max(std::clamp((distance({px,py},{120.8f,98.1f})-37.3f)*ui_art_scale+.5f,0.f,1.f),std::clamp((15-distance({px,py},{120.5f,100}))*ui_art_scale+.5f,0.f,1.f));
                pixels[p+3]=std::byte(unsigned(std::lround(gauge.face_alpha*a)));
            }
        }
        rim(paired,52,151,389);
        shade_rim(paired,52.5f);
        ticks(paired,45,gauge.layout==2?162.f:158.f,gauge.layout==2?24.f:20.4f,gauge.layout==2?10:12,false);
    }

    // Squared, slanted numerals belong to the single-dial dashboard. The
    // overlapping dials use the game's condensed face instead.
    constexpr std::string_view figures[]={
        "M1 0 L4 0 C4.8 0 5 .5 5 1.3 L5 5.7 C5 6.5 4.8 7 4 7 L1 7 C.2 7 0 6.5 0 5.7 L0 1.3 C0 .5 .2 0 1 0 Z",
        "M0 0 L1.3 0 L1.3 7",
        "M0 1.5 L0 .7 C0 .2 .3 0 .8 0 L4.3 0 C4.8 0 5 .2 5 .7 L5 2.7 C5 3.2 4.7 3.5 4.2 3.5 L.8 3.5 C.3 3.5 0 3.8 0 4.3 L0 7 L5 7",
        "M0 1.5 L0 .7 C0 .2 .3 0 .8 0 L4.2 0 C4.7 0 5 .3 5 .8 L5 2.4 C5 3 4.5 3.4 3.8 3.4 L2 3.4 M3.8 3.4 C4.5 3.4 5 3.8 5 4.5 L5 6.2 C5 6.7 4.7 7 4.2 7 L.8 7 C.3 7 0 6.7 0 6.2 L0 5.5",
        "M3.5 0 L5.1 0 L5.1 5.2 L6.2 5.2 L6.2 6.6 L5.1 6.6 L5.1 8.2 L3.6 8.2 L3.6 6.6 L0 6.6 L0 5.2 Z M3.6 2 L1.4 5.2 L3.6 5.2 Z",
        "M5 0 L0 0 L0 3.1 L3.9 3.1 C4.7 3.1 5 3.5 5 4.2 L5 5.9 C5 6.7 4.7 7 3.9 7 L.8 7 C.3 7 0 6.7 0 6.2 L0 5.5",
        "M5 1 L5 .7 C5 .2 4.7 0 4.2 0 L1 0 C.3 0 0 .4 0 1.1 L0 5.9 C0 6.6 .3 7 1 7 L4 7 C4.7 7 5 6.6 5 5.9 L5 4.3 C5 3.6 4.7 3.2 4 3.2 L0 3.2",
        "M0 0 L5 0 L1.7 7",
        "M1.2 0 L3.8 0 C4.6 0 5 .4 5 1.2 L5 2.1 C5 2.9 4.6 3.4 3.8 3.4 L1.2 3.4 C.4 3.4 0 2.9 0 2.1 L0 1.2 C0 .4 .4 0 1.2 0 Z M1.2 3.4 C.4 3.4 0 3.9 0 4.7 L0 5.8 C0 6.6 .4 7 1.2 7 L3.8 7 C4.6 7 5 6.6 5 5.8 L5 4.7 C5 3.9 4.6 3.4 3.8 3.4",
        "M0 7 L3.8 7 C4.6 7 5 6.5 5 5.7 L5 1.3 C5 .5 4.6 0 3.8 0 L1.2 0 C.4 0 0 .5 0 1.3 L0 2.5 C0 3.3 .4 3.8 1.2 3.8 L5 3.8"
    };
    const auto numeral=[&](unsigned digit,float x,float y,float w,float h) {
        auto edges=outline(single?figures[digit]:glyphs[digit].path,0,0);
        if(single) for(auto& e:edges) for(auto* p:{&e.a,&e.b}) p->x-=p->y*.22f;
        float left=1e6f,right=-1e6f,top=1e6f,bottom=-1e6f;
        for(const auto& e:edges) for(auto p:{e.a,e.b}) {left=std::min(left,p.x);right=std::max(right,p.x);top=std::min(top,p.y);bottom=std::max(bottom,p.y);}
        // Trace 4's flat top and crossbar as ink, avoiding the pointed miter
        // produced by stroking a triangular centreline. Other single-dial
        // digits retain their original squared strokes.
        const bool stroked=single && digit!=4;
        const float line=stroked?(h<7?1.08f:digit==1?1.85f:digit==5||digit==6?1.6f:1.5f):0;
        for(auto& e:edges) for(auto* p:{&e.a,&e.b}) *p={x+line*.5f+(p->x-left)*(w-line)/(right-left),y+line*.5f+(p->y-top)*(h-line)/(bottom-top)};
        // Cover the stroke ends too: a butt-ended black stroke leaves the
        // white top of 5 flush with the edge of its outline.
        vector_stroke(pixels,width,edges,line+1.8f,0xFF090909,stroked?.9f:0.f,true);
        if(stroked) stroke(edges,line,0xFFF4F4F4);else vector_fill(pixels,width,edges,0xFFF4F4F4);
    };
    struct Number {unsigned value;float x,y,w,h;};
    constexpr Number single8[]={{0,53.65f,96.1f,7,8},{1,38,87.4f,3,8},{2,26,68,8,8},{3,26.7f,47,7,8},{4,38.4f,28.6f,6.6f,8.6f},{5,58.8f,22,7,8},{6,81,26.1f,7,8},{7,98,40.3f,6,8},{8,102.1f,61.7f,7.7f,7.8f}};
    constexpr Number single12[]={{0,55.6f,97.05f,7,8},{1,44,92.2f,3,8},{2,30.1f,79.1f,8,8},{3,25.65f,63.05f,7,8},{4,26.1f,47.15f,7,8},
        {5,35.8f,33,7,8},{6,50,23,7,8},{7,65.9f,21.35f,6,8},{8,80,24.55f,8,8},{9,92.7f,33.95f,7,8},
        {1,98,48.2f,3,8},{0,101,48.05f,8,8},{1,102.2f,64.8f,3,8},{1,106.8f,64.85f,3,8}};
    constexpr Number speedNumbers[]={{0,37.75f,102.4f,4,7.7f},{2,24,90.7f,4,8},{0,29,90.7f,4,8},{4,16.85f,70.8f,4,8},{0,21.85f,70.8f,4,8},{6,17.85f,51.8f,4,8},{0,22.85f,51.8f,4,8},
        {8,28.3f,35.6f,4.5f,7.7f},{0,34,35.6f,3.9f,7.7f},{1,42.2f,24.8f,3,8},{0,46.2f,24.8f,4,8},{0,51.2f,24.8f,4,8},
        {1,60.6f,21.55f,3,7.6f},{2,64.6f,21.55f,4,7.6f},{0,69.6f,21.55f,4,7.6f},
        {1,79.4f,25.7f,3,7.8f},{4,83.4f,25.7f,4,7.8f},{0,88.4f,25.7f,4,7.8f},{1,94,37,3,7},{6,98,36,4,8},{0,103,36,4,8}};
    constexpr Number small10[]={{1,85.2f,107.55f,2.7f,7.7f},{2,83.7f,91.7f,4.2f,7.8f},{3,87.9f,77.3f,4.4f,8},{4,97.05f,65.9f,4.6f,7.5f},
        {5,111.5f,62,4.5f,7.6f},{6,125.05f,61.2f,4.6f,7.7f},{7,138.85f,68.7f,3.8f,7.5f},{8,149.45f,78.55f,4.4f,7.5f},
        {9,152.55f,92.85f,4.4f,7.9f},{1,149,108,3,8},{0,153,108.3f,3.6f,7.8f}};
    constexpr Number small12[]={{1,84.25f,108.7f,2.7f,7.5f},{2,82.8f,95.7f,4.2f,7.8f},{3,84.9f,82.3f,4.4f,8},{4,91.15f,71.95f,4.6f,7.5f},
        {5,100.5f,65,4.5f,7.6f},{6,112.05f,60.2f,4.6f,7.7f},{7,124.85f,59.7f,3.8f,7.5f},{8,136.35f,62.7f,4.4f,7.3f},
        {9,145.6f,70.85f,4.4f,7.9f},{1,149,83,3,8},{0,153,83.3f,3.6f,7.8f},{1,151.55f,96.25f,3,7.5f},{1,155.55f,96.3f,3,7.5f},
        {1,149.6f,109.3f,3,7.6f},{2,153.9f,109.6f,3.8f,8}};
    const auto numbers=[&](std::span<const Number> list) {for(const auto& n:list) numeral(n.value,n.x,n.y,n.w,n.h);};
    if(single) {
        numbers(gauge.layout==0?std::span<const Number>(single8):std::span<const Number>(single12));
        // Original small italic x1000rpm legend.
        const auto legend=outline(
            "M49.9 43.1 L51.25 43.1 L51.7 44.55 L52.5 43.1 L53.25 43.1 L51.95 45.15 L52.45 46.75 "
            "L53 46.75 L52.85 47.35 L51.1 47.35 L51.3 46.75 L51.5 46.75 L51.1 45.8 L50.55 46.75 "
            "L50.9 46.75 L50.7 47.35 L49.05 47.35 L49.25 46.75 L49.6 46.75 L50.8 45.05 L50.35 43.75 L49.75 43.75 Z "
            "M70.6 47.4 L71.15 43.65 L72.15 43.65 L72.05 44.1 C72.5 43.6 72.9 43.55 73.3 43.65 "
            "L73.15 44.55 C72.4 44.35 72 44.75 71.9 45.3 L71.6 47.4 Z "
            "M72.8 48.65 L73.55 43.6 L74.55 43.6 L74.5 44 C74.9 43.65 75.4 43.5 75.9 43.6 "
            "C77.6 43.7 77 46.8 75.4 47 C74.9 47 74.4 46.9 74.15 46.6 L73.85 48.65 Z "
            "M74.5 44.7 L74.25 46 C75.4 46.5 76.25 44.6 75.25 44.5 C75 44.45 74.7 44.5 74.5 44.7 Z "
            "M76.8 47.4 L77.4 43.6 L78.35 43.6 L78.3 44 C78.8 43.5 79.7 43.45 80.1 44 "
            "C80.7 43.45 81.5 43.45 82 43.6 C82.8 43.8 82.8 44.3 82.65 45.2 L82.3 47.4 L81.3 47.4 "
            "L81.65 45 C81.8 44.1 80.5 44.2 80.35 45 L80 47.4 L79 47.4 L79.35 45 "
            "C79.45 44.2 78.2 44.2 78.05 45 L77.7 47.4 Z",0,0);
        vector_stroke(pixels,width,legend,1.6f,0xFF090909,0,true);vector_fill(pixels,width,legend,0xFFE8E8E8);
        // Match the zeroes' ink bounds and outline width. A stroked centreline
        // gave the 1 a raised, sloping baseline and an oversized top join.
        const auto one=outline("M54.05 41.76 L56 41.5 L54.67 47.5 L53.6 47.5 L54.68 42.71 L54.03 42.82 Z",0,0);
        vector_stroke(pixels,width,one,1.8f,0xFF090909,0,true);vector_fill(pixels,width,one,0xFFF4F4F4);
        for(float x:{56.15f,60.65f,65.15f}) numeral(0,x,41.5f,4.3f,6);
    } else {
        numbers(speedNumbers);numbers(gauge.layout==2?std::span<const Number>(small10):std::span<const Number>(small12));
        float x=55;
        for(char letter:std::string_view("MPH")) {
            const auto& glyph=glyphs[37+letter-'A'];auto edges=outline(glyph.path,0,0);
            for(auto& e:edges) for(auto* p:{&e.a,&e.b}) *p={x+p->x*.45f,42.9f+p->y*.52f};
            stroke(edges,.85f,0xFF080808);vector_fill(pixels,width,edges,0xFFE9E9E9);x+=letter=='M'?8:6;
        }
        ellipse(pixels,width,139.5f,114.5f,4.2f,4.2f,[](float,float){return Color{5,5,5};});
        ellipse(pixels,width,139.5f,114.5f,3.3f,3.3f,[](float u,float v){const float c=70-8*(u+v);return Color{c,c,c};});
    }
    // Measured axial brightness/opacity, without the BC3 block fluctuations.
    // Both needles retain their original centers, taper and black end caps.
    const auto ramp=[](float y,std::span<const Point> stops) {
        for(unsigned i=1;i<stops.size();++i) if(y<stops[i].x) {
            const float t=std::clamp((y-stops[i-1].x)/(stops[i].x-stops[i-1].x),0.f,1.f);
            return stops[i-1].y+(stops[i].y-stops[i-1].y)*t;
        }
        return stops.back().y;
    };
    constexpr Point brightness[]={{4.5f,8},{8.5f,110},{16.5f,150},{20.5f,154},{28.5f,239},{34.5f,255},{68.5f,255},{69,0}};
    constexpr Point opacity[]={{4.5f,60},{8.5f,113},{18.5f,144},{25.5f,217},{40.5f,230},{52.5f,253}};
    for(unsigned y=4*ui_art_scale;y<70*ui_art_scale;++y) {
        const float py=(y+.5f)/ui_art_scale,t=std::clamp((py-4)/65,0.f,1.f);
        const float half=6.4f-3.4f*t,white_half=6.1f-3.6f*t,core=std::max(1.f,3.5f-6*t);
        const float alpha_core=t<.35f?3.5f-2.3f*t/.35f:1.2f+1.2f*(t-.35f)/.65f;
        const float light=ramp(py,brightness),alpha=ramp(py,opacity);
        for(unsigned x=213*ui_art_scale;x<227*ui_art_scale;++x) {
            const float dx=std::abs((x+.5f)/ui_art_scale-220);
            const float a=alpha*std::pow(std::clamp((half-dx)/(half-alpha_core),0.f,1.f),1.2f);
            const float c=light*std::clamp((white_half-dx)/(white_half-core),0.f,1.f);
            const auto p=(std::size_t(y)*width+x)*4;for(unsigned ch=0;ch<3;++ch) pixels[p+ch]=std::byte(unsigned(std::lround(c)));
            pixels[p+3]=std::byte(unsigned(std::lround(a)));
        }
    }
    fill("M242 4 L246 4 L247 70 L241 70 Z",0xFF080808);
    fill("M243 5 L245 5 L246 69 L242 69 Z",0xFFFFFFFF);
    if(!single) for(unsigned y=108*ui_art_scale;y<125*ui_art_scale;++y) for(unsigned x=236*ui_art_scale;x<253*ui_art_scale;++x) {
        const float r=distance({(x+.5f)/ui_art_scale,(y+.5f)/ui_art_scale},{244.5f,116.5f});
        const float a=std::exp(-1.2f*std::max(0.f,r-4))*std::clamp(8-r,0.f,1.f);
        const auto p=(std::size_t(y)*width+x)*4;
        for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte{255};
        pixels[p+3]=std::byte(unsigned(std::lround(a*255)));
    }
}

void boost_art(std::vector<std::byte>& pixels,unsigned width) {
    constexpr std::string_view silhouette="M1.2 31.5 C2.4 14.3 19 .5 37 .5 L126 .5 L126 31.5 Z";
    constexpr std::string_view rim="M126 3.1 L37 3.1 C20 3.1 5.1 14.9 3.4 29.5 L126 29.5";
    const auto frame=outline(rim,0,0);
    // The coloured bars and the flame's alpha mask share the frame's inner
    // edge. Independently traced curves left a gap above and beside the fire.
    std::vector<Edge> interior;Point first{},previous{};
    const auto normal=[](const Edge& e) {
        const float dx=e.b.x-e.a.x,dy=e.b.y-e.a.y,length=std::hypot(dx,dy);
        return Point{-dy/length,dx/length};
    };
    for(std::size_t i=0;i<=frame.size();++i) {
        const auto p=i==frame.size()?frame.back().b:frame[i].a;
        const auto a=normal(frame[i?i-1:0]),b=normal(frame[std::min(i,frame.size()-1)]);
        const float scale=2/(1+a.x*b.x+a.y*b.y); // Half of the four-pixel rim.
        const Point inset{p.x-(a.x+b.x)*scale,p.y-(a.y+b.y)*scale};
        if(i) interior.push_back({previous,inset});else first=inset;
        previous=inset;
    }
    interior.push_back({previous,first});
    const auto fill_interior=[&](float y,std::uint32_t ink) {
        auto edges=interior;for(auto& e:edges) {e.a.y+=y;e.b.y+=y;}
        vector_fill(pixels,width,edges,ink);
    };
    for(unsigned row=0;row<3;++row) {
        const float y=float(row*32);
        vector_fill(pixels,width,outline(silhouette,0,y),0x7F000000);
        if(row) fill_interior(y,0xFF000097);
        if(row) for(unsigned yy=row*32*ui_art_scale;yy<(row+1)*32*ui_art_scale;++yy) for(unsigned x=0;x<width;++x) {
            const auto p=(std::size_t(yy)*width+x)*4;if(pixels[p]==std::byte{}) continue;
            const float px=(x+.5f)/ui_art_scale;
            Color color=px<90?Color{0,0,151}:Color{129,60,22};
            if(row==2) {
                // Original four-pixel period: bevel, recessed gap, two-pixel
                // face. Keep the blue recess blue, not a new black separator.
                const float part=std::fmod(px,4.f),py=(yy+.5f)/ui_art_scale-y;
                if(part<1) color=px<90?Color{99,117,200}:Color{140,68,27};
                else if(part<2) color=px<90?Color{16,18,156}:Color{8,4,16};
                else color=px<90?Color{140,162,222}:Color{206,97,33};
                const float bevel=.35f+.65f*std::clamp(std::min(py-5.1f,27.5f-py),0.f,1.f);
                for(auto& c:color) c*=bevel;
            }
            for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte(unsigned(color[2-c]));
        }
        auto edges=outline(rim,0,y);
        vector_stroke(pixels,width,edges,4,0xFF080808);
        const auto shine=[&](float size,std::uint32_t ink) {
            vector_stroke_width(pixels,width,edges,[=](Point p) {return size*(1-.28f*std::clamp((p.y-y-25)/4.5f,0.f,1.f));},ink);
        };
        shine(2.5f,0xFF404040);shine(1.8f,0xFF9C9C9C);shine(.9f,0xFFF5F5F5);
        for(unsigned yy=row*32*ui_art_scale;yy<(row+1)*32*ui_art_scale;++yy) for(unsigned x=0;x<width;++x) {
            const auto p=(std::size_t(yy)*width+x)*4;
            if(pixels[p]!=pixels[p+1] || pixels[p]!=pixels[p+2]) continue;
            const float bottom=std::clamp(((yy+.5f)/ui_art_scale-y-25)/4.5f,0.f,1.f);
            const float light=(1-.46f*(x+.5f)/width)*(1-bottom)+.6f*bottom;
            for(unsigned c=0;c<3;++c) pixels[p+c]=std::byte(unsigned(std::lround(unsigned(pixels[p+c])*light)));
        }
        // Five authored gray marks on the upper chrome, shared by all states.
        // Original bounds are x80..84 through x112..116, y2..4 in each row.
        constexpr unsigned shades[]={74,70,67,64,62};
        for(unsigned i=0;i<std::size(shades);++i) {
            const auto mark=outline("M0 0 L4 0 L4 2 L0 2 Z",float(80+i*8),y+2);
            vector_fill(pixels,width,mark,0xFF000000|(shades[i]*0x010101));
        }
    }
    fill_interior(96,0xFF000000);
}

UiArt mip_chain(std::vector<std::byte> pixels,unsigned width,unsigned height,bool font) {
    UiArt result{font?TextureFormat::dxt5:TextureFormat::argb8_linear,{}};
    result.bytes.reserve(std::size_t(width)*height*(font?1:4)*85/64);
    for(unsigned mip=0;mip<ui_art_mips;++mip) {
        if(font) {
            const auto blocks=compress_font(pixels,width,height);
            result.bytes.insert(result.bytes.end(),blocks.begin(),blocks.end());
        } else result.bytes.insert(result.bytes.end(),pixels.begin(),pixels.end());
        if(mip+1==ui_art_mips) break;
        // Average coverage before BC3 compression, including transparent pixels.
        // Color is alpha-weighted to avoid dark fringes around colored buttons.
        // Stop at the original atlas size so adjacent glyphs cannot bleed through
        // coarser levels. Shrink in place; no full-size mip scratch allocation.
        for(unsigned y=0;y<height/2;++y) for(unsigned x=0;x<width/2;++x) {
            const auto p=(std::size_t(y)*2*width+x*2)*4,out=(std::size_t(y)*(width/2)+x)*4;
            const std::size_t taps[]={p,p+4,p+width*4,p+width*4+4};
            unsigned alpha=0;for(auto tap:taps) alpha+=unsigned(pixels[tap+3]);
            for(unsigned c=0;c<3;++c) {
                unsigned color=0;if(!font) for(auto tap:taps) color+=unsigned(pixels[tap+c])*(alpha?unsigned(pixels[tap+3]):1);
                const unsigned weight=alpha?alpha:4;
                pixels[out+c]=std::byte(font?255:(color+weight/2)/weight);
            }
            pixels[out+3]=std::byte((alpha+2)/4);
        }
        width/=2;height/=2;pixels.resize(std::size_t(width)*height*4);
    }
    return result;
}
}

std::optional<UiArt> redraw_ui_texture(const TextureData& t) {
    if(t.format!=TextureFormat::dxt5 || t.mip_levels!=1 || t.depth!=1 || t.cubemap || t.pitch ||
       (t.height!=128 && t.height!=256) || (t.width!=128 && t.width!=256) || t.bytes.size()!=std::size_t(t.width)*t.height) return {};
    Sha1 sha;sha.reset();sha.update(t.bytes);const auto digest=hex_bytes(sha.finish());
    const bool font=t.width==256 && t.height==256 && digest=="8BD1B7A660160D24E69B109E91956DACA2D4D726";
    const bool parts=t.width==128 && t.height==256 && digest=="8781ACD85F32C81754758FA8EEB9A06956A3DC3E";
    const bool icons=t.width==128 && t.height==128 && digest=="490262F84ECBE5B9A723FF40CA0C90021E4C59BB";
    const bool boost=t.width==128 && t.height==128 && digest=="4809BAAF2FB91516878C68BC786E996E7E9E9F5D";
    const Gauge* gauge=nullptr;
    if(t.width==256 && t.height==128) for(const auto& candidate:gauges) if(candidate.hash==digest) {gauge=&candidate;break;}
    if(!font && !parts && !icons && !boost && !gauge) return {};
    const auto width=t.width*ui_art_scale,height=t.height*ui_art_scale;
    auto pixels=(icons || boost || gauge)?std::vector<std::byte>(std::size_t(width)*height*4):enlarge(decode(t),t.width,t.height,font);
    if(font) for(const auto& glyph:glyphs) paint(pixels,width,glyph.bounds,outline(glyph.path,glyph.x,glyph.y));
    else if(icons) buttons(pixels,width);
    else if(gauge) gauge_art(pixels,width,*gauge);
    else if(boost) boost_art(pixels,width);
    else {
        paint(pixels,width,{0,62,80,44},outline(
            "M10 63 L69 63 C74 63 79 68 79 73 L79 95 C79 100 74 105 69 105 L10 105 C5 105 1 100 1 95 L1 73 C1 68 5 63 10 63 Z "
            "M10 65 L69 65 C73 65 77 69 77 73 L77 95 C77 99 73 103 69 103 L10 103 C6 103 3 99 3 95 L3 73 C3 69 6 65 10 65 Z",0,0));
        paint(pixels,width,{0,106,80,43},outline(
            "M10 106.5 L69 106.5 C75 106.5 79 111 79 117 L79 138 C79 144 75 148.5 69 148.5 L10 148.5 C4 148.5 .5 144 .5 138 L.5 117 C.5 111 4 106.5 10 106.5 Z",0,0));
        paint(pixels,width,{0,149,89,53},outline(
            "M15.5 149.2 L72.5 149.2 C81 149.2 86.7 157 86.7 166 L86.7 185 C86.7 194 81 200.65 72.5 200.65 L15.5 200.65 C7 200.65 1.3 194 1.3 185 L1.3 166 C1.3 157 7 149.2 15.5 149.2 Z "
            "M15.5 151.5 L72.5 151.5 C79.7 151.5 84.4 158.3 84.4 166 L84.4 185 C84.4 192.7 79.7 198.35 72.5 198.35 L15.5 198.35 C8.3 198.35 3.6 192.7 3.6 185 L3.6 166 C3.6 158.3 8.3 151.5 15.5 151.5 Z",0,0));
        paint(pixels,width,{80,62,25,44},outline("M104 63 L104 105 L81 84 Z",0,0));
        paint(pixels,width,{79,106,26,43},outline("M80 107 L103 127.5 L80 148 Z",0,0));
        paint(pixels,width,{91,149,17,10},outline("M99.5 150 L107 157.5 L92 157.5 Z",0,0));
        paint(pixels,width,{108,159,9,16},outline("M109 160 L116 167 L109 174 Z",0,0));
        wreath_art(pixels,width);
    }
    return mip_chain(std::move(pixels),width,height,font);
}
}
