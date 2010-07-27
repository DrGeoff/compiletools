CAKE_CC = ccache g++ -I . -I deps/3rdparty/lwip -I ./deps/3rdparty/lwip/lwip/src/include/ -I ./deps/3rdparty/lwip/lwip/src/include/ipv4/
CAKE_CXXFLAGS=-fPIC -g
CAKE_LINKFLAGS=-fPIC

CAKE_DEBUG_CC = $CAKE_CC
CAKE_DEBUG_CXXFLAGS=$CAKE_CXXFLAGS -Wall
CAKE_DEBUG_LINKFLAGS=$CAKE_LINKFLAGS -Wall

CAKE_RELEASE_CC = $CAKE_CC -O3
CAKE_RELEASE_CXXFLAGS=-fPIC -O3 -DNDEBUG -Wall
CAKE_RELEASE_LINKFLAGS=-O3 -DNDEBUG -Wall
