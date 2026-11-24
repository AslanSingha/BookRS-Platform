package Koha::Plugin::Com::BookRS::Widget;

# BookRS-Platform recommendations widget for the Koha OPAC.
#
# Emits one <script> tag at the bottom of every OPAC page, carrying the
# per-request CSP nonce, that loads widget.js from the BookRS API. The
# widget itself decides what to do on each page: related works on
# opac-detail.pl, semantic suggestions on opac-search.pl.

use Modern::Perl;
use base qw(Koha::Plugins::Base);

use C4::Context;
use Koha::ContentSecurityPolicy;

our $VERSION = "0.2.0";

our $metadata = {
    name            => 'BookRS recommendations',
    author          => 'Rin Singh',
    date_authored   => '2026-09-18',
    date_updated    => '2026-11-24',
    minimum_version => '23.11.00.000',
    maximum_version => undef,
    version         => $VERSION,
    description     => 'Adds BookRS-Platform recommendations to the OPAC: '
                     . 'related works on record pages, and suggestions by '
                     . 'meaning on search pages (including when the search '
                     . 'found nothing), and recommendations for the logged-in '
                     . 'patron from their own ratings. Requires a running '
                     . 'BookRS-Platform API.',
};

my @SETTINGS = qw(api_url source_id limit min_score heading internal_api_url);

# Never shown back in the form, and never sent to a browser. Left
# unchanged when the form is saved with the field empty.
my @SECRETS = qw(service_token patron_secret);

my $DEFAULT_INTERNAL = 'http://host.docker.internal:8000';

sub new {
    my ( $class, $args ) = @_;
    $args->{metadata} = $metadata;
    $args->{metadata}->{class} = $class;
    return $class->SUPER::new($args);
}

sub install {
    my ($self) = @_;
    $self->store_data(
        {   api_url   => 'http://localhost:8000',
            source_id => '1',
            limit     => '6',
            min_score => '0.55',
            heading   => '',
            internal_api_url => $DEFAULT_INTERNAL,
        }
    ) unless defined $self->retrieve_data('api_url');
    return 1;
}

sub upgrade {
    my ($self) = @_;
    $self->store_data( { internal_api_url => $DEFAULT_INTERNAL } )
        unless defined $self->retrieve_data('internal_api_url');
    return 1;
}
sub uninstall { return 1; }

sub configure {
    my ($self) = @_;
    my $cgi      = $self->{cgi};
    my $template = $self->get_template( { file => 'configure.tt' } );

    if ( ( $cgi->param('op') // '' ) eq 'cud-save' ) {
        my %new;
        for my $key (@SETTINGS) {
            my $v = $cgi->param($key);
            $v = '' unless defined $v;
            $v =~ s/^\s+|\s+$//g;
            $new{$key} = $v;
        }
        $new{limit}     = '6'    unless $new{limit}     =~ /^\d{1,2}$/;
        $new{min_score} = '0.55' unless $new{min_score} =~ /^(0(\.\d+)?|1(\.0+)?)$/;
        $new{source_id} = ''     unless $new{source_id} =~ /^\d*$/;
        $new{internal_api_url} = '' unless $new{internal_api_url} =~ m{^(https?://\S+)?$};
        for my $key (@SECRETS) {
            my $v = $cgi->param($key) // '';
            $v =~ s/^\s+|\s+$//g;
            $new{$key} = $v if length $v;
        }
        $new{$_} = '' for grep { ( $cgi->param("clear_$_") // '' ) eq '1' } @SECRETS;
        $self->store_data( \%new );
        $template->param( saved => 1 );
    }

    $template->param( map { $_ => ( $self->retrieve_data($_) // '' ) } @SETTINGS );
    $template->param( map { ( "${_}_set" => length( $self->retrieve_data($_) // '' ) ? 1 : 0 ) } @SECRETS );
    $self->output_html( $template->output() );
}

# /api/v1/contrib/bookrs/... -- see Widget/openapi.json and
# Widget/Controller.pm.
sub api_namespace { return 'bookrs'; }

sub api_routes {
    my ($self) = @_;
    require Mojo::JSON;
    return Mojo::JSON::decode_json( $self->mbf_read('openapi.json') );
}

sub _esc {
    my ($s) = @_;
    $s = '' unless defined $s;
    $s =~ s/&/&amp;/g; $s =~ s/"/&quot;/g; $s =~ s/</&lt;/g; $s =~ s/>/&gt;/g;
    return $s;
}

sub opac_js {
    my ($self) = @_;

    my $api = $self->retrieve_data('api_url') // '';
    $api =~ s{\s+}{}g;
    $api =~ s{/+$}{};
    return q{} unless $api =~ m{^https?://};

    my $nonce = eval { Koha::ContentSecurityPolicy->new->get_nonce } // '';

    my @attrs = ( 'src' => "$api/widget.js", 'data-api' => $api );
    my $source = $self->retrieve_data('source_id') // '';
    push @attrs, 'data-source-id' => $source if length $source;
    push @attrs, 'data-limit'     => ( $self->retrieve_data('limit')     // '6' );
    push @attrs, 'data-min-score' => ( $self->retrieve_data('min_score') // '0.55' );
    my $heading = $self->retrieve_data('heading') // '';
    push @attrs, 'data-heading' => $heading if length $heading;
    push @attrs, 'nonce' => $nonce if length $nonce;

    my $tag = '<script';
    while (@attrs) {
        my ( $k, $v ) = splice( @attrs, 0, 2 );
        $tag .= qq{ $k="} . _esc($v) . q{"};
    }
    return $tag . '></script>';
}

1;
