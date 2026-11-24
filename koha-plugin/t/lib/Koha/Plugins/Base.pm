package Koha::Plugins::Base;
# Test stand-in for Koha's plugin base class: settings in memory, and
# mbf_read from the plugin's bundle directory as Koha does.
use Modern::Perl;
our %DATA;
sub new { my ( $class, $args ) = @_; return bless { %{ $args // {} } }, $class }
sub retrieve_data { my ( $self, $k ) = @_; return $DATA{$k} }
sub store_data { my ( $self, $h ) = @_; @DATA{ keys %$h } = values %$h; return 1 }
sub mbf_read {
    my ( $self, $file ) = @_;
    ( my $path = ref($self) ) =~ s{::}{/}g;
    ( my $dir = $INC{"$path.pm"} ) =~ s/\.pm$//;
    open my $fh, '<:raw', "$dir/$file" or die "$dir/$file: $!";
    local $/;
    return <$fh>;
}
1;
