#!/usr/bin/env perl
# Native replacement for libdisplay-info's Python PNP-name table generator.
use strict;
use warnings;
use Encode qw(encode_utf8);
@ARGV == 3 or die "Usage: $0 INPUT OUTPUT IDENTIFIER\n";
my ($input, $output, $identifier) = @ARGV;
open my $in, '<:encoding(UTF-8)', $input or die "$input: $!";
my %names;
while (<$in>) {
    my ($key, $value) = /^(\S+)\s+(.+?)\s*$/;
    next unless defined($value) && length($key) == 3;
    $value = encode_utf8($value);
    $value =~ s/([^A-Za-z0-9 .,])/sprintf('\\%03o', ord($1))/ge;
    $names{$key} = $value;
}
close $in;
open my $out, '>:encoding(UTF-8)', $output or die "$output: $!";
print $out "#include <stdint.h>\n#include <string.h>\nconst char *$identifier(const char *);\nconst char *$identifier(const char *key) {\n    if (strlen(key) > 4) return NULL;\n    uint32_t code = 0;\n    for (; *key; ++key) code = (code << 8) | (uint8_t)*key;\n    switch (code) {\n";
for my $key (sort keys %names) {
    my $code = 0;
    $code = ($code << 8) | ord($_) for split //, $key;
    print $out "    case $code: return \"$names{$key}\";\n";
}
print $out "    default: return NULL;\n    }\n}\n";
close $out or die "$output: $!";
