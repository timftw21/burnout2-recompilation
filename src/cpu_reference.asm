; Independent host CPU references for offline guest instruction diagnostics.
; All procedures preserve Windows x64 nonvolatile registers and clear DF.
.code
flags_reference macro symbol:req, operation:req, first:req, second:req
symbol proc
    operation first, second
    pushfq
    pop rax
    ret
symbol endp
endm

flags_reference b2_add8_flags, add, cl, dl
flags_reference b2_add16_flags, add, cx, dx
flags_reference b2_add32_flags, add, ecx, edx
flags_reference b2_compare8_flags, cmp, cl, dl
flags_reference b2_compare16_flags, cmp, cx, dx
flags_reference b2_compare32_flags, cmp, ecx, edx
flags_reference b2_and8_flags, and, cl, dl
flags_reference b2_and16_flags, and, cx, dx
flags_reference b2_and32_flags, and, ecx, edx

carry_reference macro symbol:req, operation:req, first:req, second:req
symbol proc
    bt r8d, 0
    operation first, second
    pushfq
    pop rax
    ret
symbol endp
endm
carry_reference b2_adc8_flags, adc, cl, dl
carry_reference b2_adc16_flags, adc, cx, dx
carry_reference b2_adc32_flags, adc, ecx, edx
carry_reference b2_sbb8_flags, sbb, cl, dl
carry_reference b2_sbb16_flags, sbb, cx, dx
carry_reference b2_sbb32_flags, sbb, ecx, edx

unary_reference macro symbol:req, operation:req, operand:req
symbol proc
    bt edx, 0
    operation operand
    pushfq
    pop rax
    ret
symbol endp
endm
unary_reference b2_inc8_flags, inc, cl
unary_reference b2_inc16_flags, inc, cx
unary_reference b2_inc32_flags, inc, ecx
unary_reference b2_dec8_flags, dec, cl
unary_reference b2_dec16_flags, dec, cx
unary_reference b2_dec32_flags, dec, ecx
unary_reference b2_neg8_flags, neg, cl
unary_reference b2_neg16_flags, neg, cx
unary_reference b2_neg32_flags, neg, ecx

shift_reference macro symbol:req, operation:req, operand:req, result:req
symbol proc
    mov eax, ecx
    mov ecx, edx
    push r8
    popfq
    operation operand, cl
    pushfq
    pop r9
    result
    shl r9, 32
    or rax, r9
    ret
symbol endp
endm
shift_reference b2_shl8, shl, al, <movzx eax, al>
shift_reference b2_shl16, shl, ax, <movzx eax, ax>
shift_reference b2_shl32, shl, eax, <mov eax, eax>
shift_reference b2_shr8, shr, al, <movzx eax, al>
shift_reference b2_shr16, shr, ax, <movzx eax, ax>
shift_reference b2_shr32, shr, eax, <mov eax, eax>
shift_reference b2_sar8, sar, al, <movzx eax, al>
shift_reference b2_sar16, sar, ax, <movzx eax, ax>
shift_reference b2_sar32, sar, eax, <mov eax, eax>
shift_reference b2_ror8, ror, al, <movzx eax, al>
shift_reference b2_ror16, ror, ax, <movzx eax, ax>
shift_reference b2_ror32, ror, eax, <mov eax, eax>
shift_reference b2_rcr8, rcr, al, <movzx eax, al>
shift_reference b2_rcr16, rcr, ax, <movzx eax, ax>
shift_reference b2_rcr32, rcr, eax, <mov eax, eax>
shift_reference b2_rol8, rol, al, <movzx eax, al>
shift_reference b2_rol16, rol, ax, <movzx eax, ax>
shift_reference b2_rol32, rol, eax, <mov eax, eax>
shift_reference b2_rcl8, rcl, al, <movzx eax, al>
shift_reference b2_rcl16, rcl, ax, <movzx eax, ax>
shift_reference b2_rcl32, rcl, eax, <mov eax, eax>

double_reference MACRO symbol,operation,destination,source,result
symbol PROC
    mov eax,ecx
    mov ecx,r8d
    push r9
    popfq
    operation destination,source,cl
    pushfq
    pop r9
    result
    shl r9,32
    or rax,r9
    ret
symbol ENDP
ENDM
double_reference b2_shld16,shld,ax,dx,<movzx eax,ax>
double_reference b2_shld32,shld,eax,edx,<mov eax,eax>
double_reference b2_shrd16,shrd,ax,dx,<movzx eax,ax>
double_reference b2_shrd32,shrd,eax,edx,<mov eax,eax>

combine16 macro
    movzx eax, ax
    movzx edx, dx
    shl edx, 16
    or eax, edx
endm
combine32 macro
    shl rdx, 32
    or rax, rdx
endm
multiply_reference macro symbol:req, operation:req, operand:req, combine:req
symbol proc
    mov eax, ecx
    operation operand
    pushfq
    pop r9
    mov [r8], r9d
    combine
    ret
symbol endp
endm
multiply_reference b2_mul8, mul, dl, <movzx eax, ax>
multiply_reference b2_imul8, imul, dl, <movzx eax, ax>
multiply_reference b2_mul16, mul, dx, combine16
multiply_reference b2_imul16, imul, dx, combine16
multiply_reference b2_mul32, mul, edx, combine32
multiply_reference b2_imul32, imul, edx, combine32

divide_reference macro symbol:req, operation:req, width:req, operand:req
symbol proc
    mov eax, ecx
    mov r8d, edx
    mov rdx, rcx
    shr rdx, width
    operation operand
    if width eq 8
        movzx edx, ah
        movzx eax, al
    elseif width eq 16
        movzx eax, ax
        movzx edx, dx
    endif
    combine32
    ret
symbol endp
endm
divide_reference b2_div8, div, 8, r8b
divide_reference b2_idiv8, idiv, 8, r8b
divide_reference b2_div16, div, 16, r8w
divide_reference b2_idiv16, idiv, 16, r8w
divide_reference b2_div32, div, 32, r8d
divide_reference b2_idiv32, idiv, 32, r8d

b2_string_move proc
    push rsi
    push rdi
    mov rdi, rcx
    mov rsi, rdx
    mov ecx, r8d
    cld
    cmp dword ptr [rsp+56], 0
    je move_width
    std
move_width:
    cmp r9d, 1
    je move_byte
    cmp r9d, 2
    je move_word
    rep movsd
    jmp move_done
move_byte:
    rep movsb
    jmp move_done
move_word:
    rep movsw
move_done:
    cld
    pop rdi
    pop rsi
    ret
b2_string_move endp

b2_string_fill proc
    push rdi
    mov rdi, rcx
    mov eax, edx
    mov ecx, r8d
    cld
    cmp dword ptr [rsp+48], 0
    je fill_width
    std
fill_width:
    cmp r9d, 1
    je fill_byte
    cmp r9d, 2
    je fill_word
    rep stosd
    jmp fill_done
fill_byte:
    rep stosb
    jmp fill_done
fill_word:
    rep stosw
fill_done:
    cld
    pop rdi
    ret
b2_string_fill endp
end
