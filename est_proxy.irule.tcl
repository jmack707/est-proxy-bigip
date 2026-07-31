when RULE_INIT {
    array set static::est_label_pools {
        "" "/Common/est-backend-pool"
    }
    set static::est_base "/.well-known/est"
}

when HTTP_REQUEST {
    set uri  [string tolower [HTTP::path]]
    set meth [HTTP::method]

    if { not ([string match "$static::est_base/*" $uri] or ($uri eq $static::est_base)) } {
        HTTP::respond 404 content "Not Found" noserver
        return
    }

    set rest [string trimleft [substr $uri [string length $static::est_base]] "/"]
    set segs [split $rest "/"]
    set first [lindex $segs 0]

    set ops {cacerts simpleenroll simplereenroll fullcmc serverkeygen csrattrs}

    if { [lsearch -exact $ops $first] >= 0 } {
        set label ""
        set op $first
    } elseif { [llength $segs] >= 2 && [lsearch -exact $ops [lindex $segs 1]] >= 0 } {
        set label $first
        set op [lindex $segs 1]
    } else {
        HTTP::respond 404 content "Unknown EST operation" noserver
        return
    }

    switch -- $op {
        cacerts  -
        csrattrs {
            if { $meth ne "GET" } {
                HTTP::respond 405 content "Method Not Allowed" noserver Allow "GET"
                return
            }
        }
        simpleenroll -
        fullcmc      -
        serverkeygen {
            if { $meth ne "POST" } {
                HTTP::respond 405 content "Method Not Allowed" noserver Allow "POST"
                return
            }
            set ct [string tolower [HTTP::header value Content-Type]]
            if { $ct ne "application/pkcs10" && ![string match "multipart/*" $ct] } {
                HTTP::respond 400 content "Bad Content-Type for $op" noserver
                return
            }
        }
        simplereenroll {
            if { $meth ne "POST" } {
                HTTP::respond 405 content "Method Not Allowed" noserver Allow "POST"
                return
            }
            if { [SSL::cert count] == 0 } {
                HTTP::respond 401 content "Client certificate required for reenroll" noserver
                return
            }
        }
    }

    HTTP::header remove "X-SSL-Client-Cert"
    HTTP::header remove "X-SSL-Client-Verify"
    HTTP::header remove "X-SSL-Client-Subject"
    HTTP::header remove "X-SSL-Client-Serial"

    if { [SSL::cert count] > 0 } {
        HTTP::header insert "X-SSL-Client-Cert"    [URI::encode [X509::whole [SSL::cert 0]]]
        HTTP::header insert "X-SSL-Client-Verify"  [SSL::verify_result]
        HTTP::header insert "X-SSL-Client-Subject" [X509::subject [SSL::cert 0]]
        HTTP::header insert "X-SSL-Client-Serial"  [X509::serial_number [SSL::cert 0]]
    }

    if { [info exists static::est_label_pools($label)] } {
        pool $static::est_label_pools($label)
    } elseif { [info exists static::est_label_pools()] } {
        # empty index, no quotes: a quoted ("") index is the literal
        # two-character key "" in Tcl, which never matches the default
        # entry registered in RULE_INIT
        pool $static::est_label_pools()
    } else {
        HTTP::respond 404 content "Unknown EST label: $label" noserver
        return
    }

    log local0.info "EST proxy: op=$op label=$label client=[IP::client_addr] cert_present=[expr {[SSL::cert count] > 0}]"
}

when HTTP_RESPONSE {
    set rct [string tolower [HTTP::header value Content-Type]]
    if { $rct ne "" && $rct ne "application/pkcs7-mime" && ![string match "application/pkcs7-mime*" $rct] && $rct ne "application/csrattrs" && ![string match "multipart/*" $rct] } {
        log local0.warn "EST proxy: unexpected response Content-Type '$rct' from pool [LB::server pool]"
    }
}
