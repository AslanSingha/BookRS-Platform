#!/usr/bin/env perl
# Exercise the plugin's API route without Koha.
#
# Koha's REST layer is replaced by the two things the controller relies
# on: an authenticated patron in the stash (here taken from a test
# header) and the openapi render key. Everything after that -- the
# settings, the hash, the call to BookRS-Platform -- is the real code,
# talking to a real platform API given in BOOKRS_TEST_API.
#
# Prints one JSON line per scenario; tests/test_koha_plugin.py checks
# them.
use Modern::Perl;
use FindBin qw($Bin);
use lib "$Bin/lib", "$Bin/..";
use Mojo::JSON qw(encode_json);
use Mojolicious::Lite;
use Test::Mojo;

use Koha::Plugin::Com::BookRS::Widget;
use Koha::Plugin::Com::BookRS::Widget::Controller;

package FakePatron { sub new { bless { n => $_[1] }, $_[0] } sub borrowernumber { $_[0]{n} } }

helper 'openapi.valid_input' => sub { $_[0] };
hook before_render => sub {
    my ( $c, $args ) = @_;
    $args->{json} = delete $args->{openapi} if exists $args->{openapi};
};
under sub {
    my $c  = shift;
    my $bn = $c->req->headers->header('X-Test-Borrower');
    $c->stash( 'koha.user' => defined $bn ? FakePatron->new($bn) : undef );
    return 1;
};
get '/recommendations' => sub { Koha::Plugin::Com::BookRS::Widget::Controller::recommendations(shift) };
get '/search'          => sub { Koha::Plugin::Com::BookRS::Widget::Controller::search(shift) };

my $t = Test::Mojo->new;
app->log->level('fatal');
my $good = {
    internal_api_url => $ENV{BOOKRS_TEST_API},
    service_token    => $ENV{BOOKRS_TEST_TOKEN},
    patron_secret    => $ENV{BOOKRS_TEST_SECRET},
    source_id        => $ENV{BOOKRS_TEST_SOURCE},
};

sub scenario {
    my ( $name, $settings, $headers, $query, $route ) = @_;
    %Koha::Plugins::Base::DATA = %$settings;
    my $tx = $t->ua->get( ( $route // '/recommendations' ) . ( $query // '' ) => $headers // {} );
    say encode_json( {
        name          => $name,
        status        => 0 + ( $tx->res->code // 0 ),
        body          => $tx->res->json,
        cache_control => $tx->res->headers->cache_control,
    } );
}

scenario( 'anonymous', $good );
scenario( 'patron',    $good, { 'X-Test-Borrower' => $ENV{BOOKRS_TEST_BORROWER} } );
scenario( 'limit',     $good, { 'X-Test-Borrower' => $ENV{BOOKRS_TEST_BORROWER} }, '?limit=2' );
scenario( 'limit_out_of_range', $good, { 'X-Test-Borrower' => $ENV{BOOKRS_TEST_BORROWER} }, '?limit=999' );
scenario( 'stranger',  $good, { 'X-Test-Borrower' => 999999 } );
scenario( 'unconfigured', { %$good, service_token => '' }, { 'X-Test-Borrower' => 1 } );
scenario( 'wrong_token',  { %$good, service_token => 'guess' }, { 'X-Test-Borrower' => 1 } );
scenario( 'wrong_secret', { %$good, patron_secret => 'other' },
    { 'X-Test-Borrower' => $ENV{BOOKRS_TEST_BORROWER} } );
scenario( 'search_anonymous', $good, {}, '?q=wizards', '/search' );
scenario( 'search_patron', $good, { 'X-Test-Borrower' => $ENV{BOOKRS_TEST_BORROWER} },
    '?q=a%20boy%20who%20is%20a%20wizard&limit=3', '/search' );
scenario( 'search_no_words', $good, { 'X-Test-Borrower' => $ENV{BOOKRS_TEST_BORROWER} }, '?q=%20x%20', '/search' );
scenario( 'platform_down', { %$good, internal_api_url => 'http://127.0.0.1:9' }, { 'X-Test-Borrower' => 1 } );

# What the plugin writes into every OPAC page, configured and not.
for my $case (
    [ 'opac_js_configured', { %$good, api_url => 'http://bookrs.test', for_you_url => '/cgi-bin/koha/opac-page.pl?page_id=1',
        semantic_url => '/cgi-bin/koha/opac-page.pl?page_id=2' } ],
    [ 'opac_js_not_personal', { api_url => 'http://bookrs.test', source_id => '54' } ],
) {
    %Koha::Plugins::Base::DATA = %{ $case->[1] };
    my $tag = Koha::Plugin::Com::BookRS::Widget->new->opac_js;
    my %attrs = $tag =~ /\s([\w-]+)="([^"]*)"/g;
    say encode_json( { name => $case->[0], attrs => \%attrs } );
}

my $routes = Koha::Plugin::Com::BookRS::Widget->new->api_routes;
my $to     = $routes->{'/recommendations'}{get}{'x-mojo-to'} // '';
my ( $class, $method ) = split /#/, $to;
say encode_json( {
    name      => 'routes',
    namespace => Koha::Plugin::Com::BookRS::Widget->api_namespace,
    to        => $to,
    resolves  => ( "Koha::Plugin::$class"->can($method) ? 1 : 0 ),
    reference => Koha::Plugin::Com::BookRS::Widget::Controller::patron_reference( 1234, 's3cret' ),
} );
