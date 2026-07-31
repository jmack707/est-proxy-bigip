# Offline regression test for est_proxy.irule.tcl. Needs only tclsh.
#
#   tclsh tests/irule_test.tcl [path/to/est_proxy.irule.tcl]
#
# Exit 0 if every case matches, 1 otherwise.
#
# The iRule file is a sequence of `when <EVENT> { ... }` commands, which is
# valid Tcl, so this defines `when` and sources the real file rather than a
# transcription of it. The TMM commands it calls are stubbed below.
#
# WHAT THIS DOES NOT COVER. It runs under stock Tcl, which differs from TMM's
# interpreter in ways that matter and have bitten this repository before:
#
#   * TMM reports `info tclversion` as 8.4; a desktop tclsh is typically 8.6.
#   * TMM's `expr` accepts the word operators `not`, `and`, `or`; stock Tcl
#     only takes !, &&, ||. They are translated below so the real source runs
#     here unmodified.
#
# So a pass here is evidence, not proof. Anything claimed as validated still
# needs a run on a real BIG-IP, with the version and date recorded --
# see CONTRIBUTING.md.

namespace eval static {}

proc when {event body} { set ::EV($event) $body }

set ::REQ(path) ""
set ::REQ(method) "GET"
set ::REQ(certs) 0
array set ::REQ_HDR {}
set ::OUT(kind) "none"
set ::OUT(detail) ""

namespace eval HTTP {
    proc path {} { return $::REQ(path) }
    proc method {} { return $::REQ(method) }
    proc header {sub args} {
        switch -- $sub {
            value  { set k [lindex $args 0]
                     if {[info exists ::REQ_HDR($k)]} { return $::REQ_HDR($k) }
                     return "" }
            remove { catch {unset ::REQ_HDR([lindex $args 0])}; return }
            insert { set ::REQ_HDR([lindex $args 0]) [lindex $args 1]; return }
        }
    }
    proc respond {code args} {
        set body ""
        for {set i 0} {$i < [llength $args]} {incr i} {
            if {[lindex $args $i] eq "content"} { set body [lindex $args [expr {$i+1}]] }
        }
        set ::OUT(kind) "respond"
        set ::OUT(detail) "$code $body"
    }
}
namespace eval SSL {
    proc cert {args} {
        if {[lindex $args 0] eq "count"} { return $::REQ(certs) }
        return "STUB-CERT"
    }
    proc verify_result {} { return 0 }
}
namespace eval X509 {
    proc whole {c} { return "PEM" }
    proc subject {c} { return "CN=stub" }
    proc serial_number {c} { return "01" }
}
namespace eval URI { proc encode {s} { return $s } }
namespace eval IP  { proc client_addr {} { return "10.0.0.1" } }

# F5's substr: substr <string> <start> ?<length>?
proc substr {s start args} {
    if {[llength $args]} { return [string range $s $start [expr {$start + [lindex $args 0] - 1}]] }
    return [string range $s $start end]
}
proc pool {name} { set ::OUT(kind) "pool"; set ::OUT(detail) $name }
proc log  {args} { }

# Word operators are an F5 extension; translate for stock Tcl. Word-boundary
# and case-sensitive, so "Not Found" and "noserver" are untouched.
proc tmm_expr_compat {body} {
    regsub -all {\mnot\M} $body {!} body
    regsub -all {\mand\M} $body {\&\&} body
    regsub -all {\mor\M}  $body {||} body
    return $body
}

set ::FAILED 0
set ::RAN 0

# The iRule ends its refusal paths with `return`. That must unwind only this
# wrapper -- evaluating the body directly inside `check` would return out of
# `check` itself, silently skipping the comparison that follows.
proc run_body {} { eval [tmm_expr_compat $::EV(HTTP_REQUEST)] }

proc check {desc path method certs ctype expected} {
    incr ::RAN
    set ::REQ(path) $path
    set ::REQ(method) $method
    set ::REQ(certs) $certs
    array unset ::REQ_HDR
    set ::REQ_HDR(Content-Type) $ctype
    set ::OUT(kind) "none"
    set ::OUT(detail) ""
    run_body
    set got "$::OUT(kind): $::OUT(detail)"
    if {$got eq $expected} {
        puts [format "  ok    %s" $desc]
    } else {
        incr ::FAILED
        puts [format "  FAIL  %s" $desc]
        puts [format "        expected: %s" $expected]
        puts [format "        got:      %s" $got]
    }
}

set irule [expr {[llength $argv] ? [lindex $argv 0] : "est_proxy.irule.tcl"}]
if {![file exists $irule]} { puts stderr "no such iRule: $irule"; exit 2 }
source $irule
eval $::EV(RULE_INIT)

set P "pool: /Common/est-backend-pool"
puts "est_proxy.irule.tcl — [file normalize $irule]"

check "unlabelled cacerts routes to default pool" \
    "/.well-known/est/cacerts" GET 0 "" $P

# Regression guard. An unknown label must fall back to the default pool. The
# quoted index static::est_label_pools("") is the literal two-character key
# "" in Tcl and never matches the empty-string key set in RULE_INIT, so this
# case returned 404 until that was fixed.
check "unknown label falls back to default pool" \
    "/.well-known/est/somelabel/cacerts" GET 0 "" $P

check "path outside /.well-known/est is refused" \
    "/nope" GET 0 "" "respond: 404 Not Found"
check "unknown operation is refused" \
    "/.well-known/est/bogusop" GET 0 "" "respond: 404 Unknown EST operation"
check "cacerts rejects POST" \
    "/.well-known/est/cacerts" POST 0 "" "respond: 405 Method Not Allowed"
check "simpleenroll rejects GET" \
    "/.well-known/est/simpleenroll" GET 0 "" "respond: 405 Method Not Allowed"
check "simpleenroll rejects wrong content-type" \
    "/.well-known/est/simpleenroll" POST 0 "text/plain" \
    "respond: 400 Bad Content-Type for simpleenroll"
check "simpleenroll accepts application/pkcs10" \
    "/.well-known/est/simpleenroll" POST 0 "application/pkcs10" $P
check "simpleenroll accepts multipart" \
    "/.well-known/est/simpleenroll" POST 0 "multipart/form-data" $P
check "simplereenroll without a client cert is refused" \
    "/.well-known/est/simplereenroll" POST 0 "application/pkcs10" \
    "respond: 401 Client certificate required for reenroll"
check "labelled simplereenroll without a client cert is refused" \
    "/.well-known/est/somelabel/simplereenroll" POST 0 "application/pkcs10" \
    "respond: 401 Client certificate required for reenroll"
check "simplereenroll with a client cert routes to the pool" \
    "/.well-known/est/simplereenroll" POST 1 "application/pkcs10" $P

puts ""
if {$::FAILED} {
    puts "$::FAILED of $::RAN case(s) FAILED"
    exit 1
}
puts "$::RAN case(s) passed"
exit 0
