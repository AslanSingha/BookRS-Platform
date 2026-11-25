package Koha::Plugin::Com::BookRS::Widget::Controller;

# One route: /api/v1/contrib/bookrs/recommendations
#
# Who is asking is decided by Koha, not by this code and not by the
# browser. Koha's REST layer authenticates the request -- from the
# patron's OPAC session cookie, or from credentials -- before this
# method runs, and leaves the patron in the stash as koha.user. A
# request nobody is logged in for has no koha.user and is refused here.
#
# The borrower number does not leave Koha. It is turned into the keyed
# hash BookRS-Platform already uses for this patron's ratings (HMAC-
# SHA256 with the shared secret, first 32 hex characters -- the same
# function as bookrs/ingestion/circulation.py's patron_reference), and
# only that reference is sent, server to server, with the service token.
# The browser receives a list of books and nothing that identifies
# anyone.

use Modern::Perl;
use Mojo::Base 'Mojolicious::Controller';

use Digest::SHA qw(hmac_sha256_hex);
use Encode qw(encode_utf8);
use Mojo::UserAgent;

use Koha::Plugin::Com::BookRS::Widget;

sub patron_reference {
    my ( $borrowernumber, $secret ) = @_;
    return substr( hmac_sha256_hex( encode_utf8("$borrowernumber"), encode_utf8($secret) ), 0, 32 );
}

sub recommendations {
    my $c = shift->openapi->valid_input or return;
    return _ask( $c, '/patrons/me/recommendations', { limit => _limit( $c, 10 ) },
        'Log in to see recommendations made for you.' );
}

# Search by meaning, reordered by this patron's taste. Same identity
# rules as above; the words go to the platform, the borrower number
# does not.
sub search {
    my $c = shift->openapi->valid_input or return;
    my $q = $c->param('q') // '';
    $q =~ s/^\s+|\s+$//g;
    unless ( length($q) >= 2 && length($q) <= 300 ) {
        return $c->render( status => 400, openapi => { error => 'Say in a few words what you are looking for.' } );
    }
    return _ask( $c, '/patrons/me/search', { q => $q, limit => _limit( $c, 20 ) },
        'Log in to have results ordered for you.' );
}

sub _limit {
    my ( $c, $default ) = @_;
    my $limit = $c->param('limit');
    $limit = $default unless defined $limit && $limit =~ /^\d+$/;
    $limit = 1  if $limit < 1;
    $limit = 24 if $limit > 24;
    return $limit;
}

sub _ask {
    my ( $c, $path, $form, $anonymous ) = @_;

    my $patron = $c->stash('koha.user');
    unless ($patron) {
        return $c->render( status => 401, openapi => { error => $anonymous } );
    }

    my $plugin = Koha::Plugin::Com::BookRS::Widget->new;
    my $api    = $plugin->retrieve_data('internal_api_url') // '';
    my $token  = $plugin->retrieve_data('service_token')    // '';
    my $secret = $plugin->retrieve_data('patron_secret')    // '';
    my $source = $plugin->retrieve_data('source_id')        // '';
    $api =~ s{\s+}{}g;
    $api =~ s{/+$}{};
    unless ( $api =~ m{^https?://} && length $token && length $secret && $source =~ /^\d+$/ ) {
        return $c->render(
            status  => 503,
            openapi => { error => 'Personal recommendations are not configured in this catalogue.' }
        );
    }

    my $ua = Mojo::UserAgent->new( connect_timeout => 3, inactivity_timeout => 10, request_timeout => 15 );
    my $tx = $ua->get(
        "$api$path" => {
            'X-BookRS-Token'  => $token,
            'X-BookRS-Patron' => patron_reference( $patron->borrowernumber, $secret ),
            'Accept'          => 'application/json',
        } => form => { %$form, source_id => $source }
    );

    my $code = $tx->res->code // 0;
    unless ($code) {
        my $why = ( $tx->error // {} )->{message} // 'no response';
        $c->app->log->warn("BookRS: platform at $api not reachable: $why");
        return $c->render(
            status  => 502,
            openapi => { error => 'The recommendation service could not be reached.' }
        );
    }

    if ( $code == 200 ) {
        $c->res->headers->cache_control('no-store, private');
        return $c->render( status => 200, openapi => $tx->res->json );
    }

    # 503 from the platform means "not enabled" or "no model yet": pass
    # the reason on. Anything else is a fault between Koha and the
    # platform (a wrong token, a bad URL) and is logged, not shown.
    if ( $code == 503 ) {
        my $detail = eval { $tx->res->json->{detail} } // 'Not available yet.';
        return $c->render( status => 503, openapi => { error => $detail } );
    }
    $c->app->log->warn("BookRS: platform answered HTTP $code for $path");
    return $c->render(
        status  => 502,
        openapi => { error => 'The recommendation service refused the request.' }
    );
}

1;
